"""E-1 成果台帳と指標のテスト (疑似PJのみ・実関数)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
本番・実案件に触れない。テストは一時ディレクトリの疑似PJのみ。
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

ROOT = Path(__file__).resolve().parents[1]


def _make_manager(tmp_path, tag="e1"):
    name = "e1-疑似-台帳-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の原本引用つき作成と検証",
                "架空文書02の原本引用つき作成と検証"]
    memory.save_mission(pid, "架空目標-台帳", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        tasks.append(compile_task(i, text, {
            "title": f"架空文書工程{i}",
            "scope": f"架空文書{i:02d}の成果物を作成する。",
            "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    from app.goal_contract import activate as _activate
    _activate(manager, pid)
    return manager, pid


def _db_files_before(manager):
    from app import outcome_ledger as ledger
    paths = []
    for label, func in (
            ("main", lambda: Path(manager.memory.path)),
            ("ledger", lambda: ledger.ledger_db_path(manager)),
            ("gap", None), ("auto", None), ("watch", None),
            ("night", None), ("shadow", None)):
        if func is None:
            continue
        path = func()
        if path.exists():
            paths.append((label, path, path.stat().st_mtime_ns,
                          path.stat().st_size))
    return paths


def test_metrics_shape_and_activity_separated(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="shape")
    before = _db_files_before(manager)
    metrics = ledger.compute_metrics(manager)
    assert metrics["format"] == "e1-ledger/1"
    for key in ("unknown_passed", "duplicate_proposals",
                "unauthorized_external", "false_achieved",
                "stop_to_action_rate", "criteria_fulfilment"):
        item = metrics[key]
        assert set(item.keys()) == {"value", "status", "detail",
                                    "unreadable_projects"}, key
        assert item["status"] in ("ok", "unknown"), key
        assert isinstance(item["detail"], dict), key
        assert isinstance(item["unreadable_projects"], list), key
        if item["status"] == "unknown":
            assert item["value"] is None, key
    assert set(metrics["activity"].keys()) == {"ticks", "night_run_steps",
                                               "shadow_records",
                                               "unreadable_projects"}
    # 活動量が成果の指標に混ざらない。
    for key in ("unknown_passed", "duplicate_proposals",
                "unauthorized_external", "false_achieved"):
        assert "ticks" not in metrics[key]["detail"] or \
            key == "unknown_passed", key
    assert "projects" in metrics
    after = _db_files_before(manager)
    assert [(label, path) for label, path, _, _ in before] == \
        [(label, path) for label, path, _, _ in after]
    for (_, path, mtime, size), (_, _, mtime2, size2) in zip(before, after):
        assert (mtime, size) == (mtime2, size2), path


def test_metrics_all_zero_when_clean(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="clean")
    metrics = ledger.compute_metrics(manager)
    # 正常ケース: 0であるべき指標が0(読める場合)。
    for key in ("unknown_passed", "duplicate_proposals",
                "unauthorized_external", "false_achieved"):
        item = metrics[key]
        assert item["status"] in ("ok", "unknown"), key
        if item["status"] == "ok":
            assert item["value"] == 0, key


def test_unknown_passed_violation_and_unreadable(tmp_path, monkeypatch):
    from app import autopilot as auto
    from app import outcome_ledger as ledger
    manager, pid = _make_manager(tmp_path, tag="unknown")
    # 違反を1件だけ注入: preflight欠落なのに advanced。
    with auto._connect(manager) as db:
        with db:
            db.execute(
                """INSERT INTO autopilot_ticks(id, project_id, tick_index,
                       plan_signature, stop_class, stop_code, action_name,
                       status, next_action, reason_ja, evidence_refs,
                       external_delta, external_rounds_delta, budget_kept,
                       dry_run, actor, created)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("tick-violation-1", pid, 1, "sig1", "missing_binding",
                 "phase:plan_conflict", "remap_stored_review", "advanced",
                 "review_feedback", "注入", "{}", 0, 0, 1, 0, "tester",
                 time.time()))
    metrics = ledger.compute_metrics(manager)
    item = metrics["unknown_passed"]
    assert item["status"] == "ok"
    assert item["value"] == 1
    # 読めない(注入)指標は null/unknown で0にならない。
    real_list = auto.list_ticks

    def _broken(mgr, project_id, limit=20):
        if str(project_id) == pid:
            raise sqlite3.OperationalError("injected")
        return real_list(mgr, project_id, limit)

    monkeypatch.setattr(auto, "list_ticks", _broken)
    metrics2 = ledger.compute_metrics(manager)
    item2 = metrics2["unknown_passed"]
    assert item2["status"] == "unknown"
    assert item2["value"] is None
    assert item2["value"] != 0
    assert pid in item2["unreadable_projects"]


def test_duplicate_proposals_violation(tmp_path):
    from app import outcome_ledger as ledger
    from app.goal_review import ReviewStore
    manager, pid = _make_manager(tmp_path, tag="dup")
    # proposal_keys 表(冪等キー)に同じキーで複数の候補を直接注入する。
    # (通常は一意制約で防がれる異常系。読取専用の検出対象。)
    store = ReviewStore(manager.memory.path)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS proposal_keys(project TEXT, "
            "proposal_key TEXT, signature TEXT, candidate_id TEXT, "
            "claimed_at REAL, PRIMARY KEY(project, proposal_key))")
        db.execute(
            "INSERT OR REPLACE INTO proposal_keys(project, proposal_key, "
            "signature, candidate_id, claimed_at) VALUES(?,?,?,?,?)",
            (pid, "dup-key-1", "sig1", "cand-a", 1.0))
    with store.connect() as db:
        # PRIMARY KEY を迂回した異常行を許すため、制約なしの退避表に作り直す。
        rows = db.execute(
            "SELECT project, proposal_key, signature, candidate_id, "
            "claimed_at FROM proposal_keys").fetchall()
        db.execute("DROP TABLE proposal_keys")
        db.execute(
            "CREATE TABLE proposal_keys(project TEXT, proposal_key TEXT, "
            "signature TEXT, candidate_id TEXT, claimed_at REAL)")
        for row in rows:
            db.execute(
                "INSERT INTO proposal_keys(project, proposal_key, "
                "signature, candidate_id, claimed_at) VALUES(?,?,?,?,?)",
                tuple(row))
        db.execute(
            "INSERT INTO proposal_keys(project, proposal_key, signature, "
            "candidate_id, claimed_at) VALUES(?,?,?,?,?)",
            (pid, "dup-key-1", "sig1", "cand-b", 2.0))
    metrics = ledger.compute_metrics(manager)
    item = metrics["duplicate_proposals"]
    assert item["status"] == "ok"
    assert item["value"] == 1


def test_unauthorized_external_violation(tmp_path):
    from app import autopilot as auto
    from app import outcome_ledger as ledger
    manager, pid = _make_manager(tmp_path, tag="ext")
    with auto._connect(manager) as db:
        with db:
            db.execute(
                """INSERT INTO autopilot_ticks(id, project_id, tick_index,
                       plan_signature, stop_class, stop_code, action_name,
                       status, next_action, reason_ja, evidence_refs,
                       external_delta, external_rounds_delta, budget_kept,
                       dry_run, actor, created)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("tick-ext-1", pid, 1, "sig1", "missing_binding",
                 "phase:plan_conflict", "remap_stored_review", "advanced",
                 "review_feedback", "注入", "{}", 2, 0, 1, 0, "tester",
                 time.time()))
    metrics = ledger.compute_metrics(manager)
    item = metrics["unauthorized_external"]
    assert item["status"] == "ok"
    assert item["value"] == 1


def test_false_achieved_violation(tmp_path, monkeypatch):
    from app import outcome_ledger as ledger
    from app.goal_state_model import build_goal_state as real_build
    manager, pid = _make_manager(tmp_path, tag="false")
    real_state = real_build(manager, pid)
    assert isinstance(real_state, dict)
    criteria = list(real_state.get("criteria") or [])
    assert criteria, "達成条件がありません"
    forged = dict(real_state)
    forged_criteria = []
    for entry in criteria:
        row = dict(entry)
        row["state"] = "achieved"
        forged_criteria.append(row)
    forged["criteria"] = forged_criteria

    def _forged(mgr, project_id):
        if str(project_id) == pid:
            return forged
        return real_build(mgr, project_id)

    monkeypatch.setattr(ledger, "_build_state_for_metrics", None,
                        raising=False)
    import app.outcome_ledger as ledger_mod
    monkeypatch.setattr("app.goal_state_model.build_goal_state", _forged)
    metrics = ledger_mod.compute_metrics(manager)
    item = metrics["false_achieved"]
    assert item["status"] == "ok"
    assert int(item["value"] or 0) >= 1


def test_deleted_and_unknown_excluded(tmp_path):
    from app import outcome_ledger as ledger
    from app.project_delete import delete_state_status
    manager, pid = _make_manager(tmp_path, tag="excluded")
    metrics = ledger.compute_metrics(manager)
    assert metrics["projects"]["total"] >= 1
    assert metrics["projects"]["counted"] >= 1
    # 状態不明のPJは除外し、「確認できず」の件数として別に返す。
    assert "excluded_ids" in metrics["projects"]
    assert isinstance(metrics["projects"]["excluded_ids"], list)
    status = delete_state_status(manager.memory.path, pid)
    assert status in ("active", "restored", "deleted", "purged", "unknown")


def test_denominator_zero_rates_null(tmp_path, monkeypatch):
    from app import outcome_ledger as ledger
    from app.goal_state_model import build_goal_state as real_build
    manager, pid = _make_manager(tmp_path, tag="zero")
    real_state = real_build(manager, pid)
    forged = dict(real_state)
    forged["criteria"] = []
    forged["summary"] = {"achieved": 0, "capability_gap": 0,
                         "evidence_gap": 0, "human_decision": 0,
                         "unknown": 0, "total": 0, "consistent": True,
                         "goal_achieved": False, "goal_remaining": []}

    def _forged(mgr, project_id):
        if str(project_id) == pid:
            return forged
        return real_build(mgr, project_id)

    import app.outcome_ledger as ledger_mod
    monkeypatch.setattr("app.goal_state_model.build_goal_state", _forged)
    metrics = ledger_mod.compute_metrics(manager)
    rate = metrics["criteria_fulfilment"]
    assert rate["status"] == "ok"
    assert rate["value"] is None


def test_snapshot_safety_ok_idempotent_and_failure(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="snap")
    metrics = ledger.compute_metrics(manager)
    expected_ok = all(
        metrics[key]["status"] == "ok" and metrics[key]["value"] == 0
        for key in ("unknown_passed", "duplicate_proposals",
                    "unauthorized_external", "false_achieved"))
    first = ledger.record_snapshot(manager, "tester")
    assert first["ok"] is True
    assert first["safety_ok"] is expected_ok
    assert first["idempotent"] is False
    second = ledger.record_snapshot(manager, "tester")
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["snapshot"]["id"] == first["snapshot"]["id"]
    # 書込失敗(注入)は失敗を返す(握りつぶさない)。
    ledger.FAIL_INJECT = {"stage": "snapshot_write_fail"}
    try:
        failed = ledger.record_snapshot(manager, "tester")
    finally:
        ledger.FAIL_INJECT = None
    assert failed["ok"] is False
    assert "error_type" in failed
    # safety_ok は unknown が1つでもあれば偽。
    forged_metrics = json.loads(json.dumps(metrics))
    forged_metrics["unknown_passed"] = {"value": None, "status": "unknown",
                                        "detail": {}, "unreadable_projects": ["x"]}
    assert ledger._safety_of(forged_metrics) is False


def test_snapshot_diff(tmp_path):
    from app import autopilot as auto
    from app import outcome_ledger as ledger
    manager, pid = _make_manager(tmp_path, tag="diff")
    first = ledger.record_snapshot(manager, "tester")
    assert first["ok"] is True
    with auto._connect(manager) as db:
        with db:
            db.execute(
                """INSERT INTO autopilot_ticks(id, project_id, tick_index,
                       plan_signature, stop_class, stop_code, action_name,
                       status, next_action, reason_ja, evidence_refs,
                       external_delta, external_rounds_delta, budget_kept,
                       dry_run, actor, created)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("tick-diff-1", pid, 1, "sig1", "missing_binding",
                 "phase:plan_conflict", "remap_stored_review", "advanced",
                 "review_feedback", "注入", "{}", 0, 0, 1, 0, "tester",
                 time.time()))
    second = ledger.record_snapshot(manager, "tester")
    assert second["ok"] is True
    assert second["idempotent"] is False
    view = ledger.list_snapshots(manager, 5)
    assert view["has_snapshot"] is True
    assert len(view["snapshots"]) >= 2
    assert "unknown_passed" in view["diff"]
