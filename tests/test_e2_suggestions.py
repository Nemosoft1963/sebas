"""E-2 方針の改善提案のテスト (疑似PJのみ・実関数)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
本番・実案件に触れない。テストは一時ディレクトリの疑似PJのみ。
"""
import hashlib
import json
import time
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox


def _make_manager(tmp_path, tag="e2"):
    name = "e2-疑似-提案-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の原本引用つき作成と検証",
                "架空文書02の原本引用つき作成と検証"]
    memory.save_mission(pid, "架空目標-提案", "\n".join(
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


def _budget_state(manager):
    from app import autopilot as auto
    from app import shadow_mode as shadow
    from app.night_run import kill_switch_state
    return {
        "kill": kill_switch_state(manager),
        "auto": auto.get_config(manager, "dummy"),
        "shadow": shadow.THRESHOLDS,
    }


def test_suggest_deterministic_and_no_auto_change(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="det")
    first = ledger.suggest(manager)
    second = ledger.suggest(manager)
    assert isinstance(first, list)
    assert first == second
    ids_first = [row["id"] for row in first]
    assert ids_first == sorted(ids_first)
    for row in first:
        assert set(row.keys()) >= {"id", "title_ja", "evidence",
                                   "suggested_human_action", "auto_change"}
        assert row["auto_change"] is False
        assert isinstance(row["id"], str) and row["id"] != ""
        assert isinstance(row["title_ja"], str) and row["title_ja"] != ""
        assert isinstance(row["evidence"], dict)
        assert "count" in row["evidence"] or "project_ids" in row["evidence"]
        # 本文なし: 件数とIDのみ。
        blob = json.dumps(row, ensure_ascii=False)
        assert "statement" not in blob
        assert "content" not in blob or "suggested_human_action" in blob
        assert isinstance(row["suggested_human_action"], str)
        assert row["suggested_human_action"] != ""


def test_suggest_same_input_same_id(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="same")
    first = {row["id"]: row for row in ledger.suggest(manager)}
    second = {row["id"]: row for row in ledger.suggest(manager)}
    assert set(first.keys()) == set(second.keys())
    for key in first:
        assert first[key] == second[key]


def test_suggest_no_config_change_proposals(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="cfg")
    rows = ledger.suggest(manager)
    blob = json.dumps(rows, ensure_ascii=False)
    for word in ("キルスイッチ", "kill_switch", "kill-switch", "予算",
                 "許可リスト", "ALLOWED_ACTIONS", "auto_change\": true",
                 "auto_change\":true"):
        assert word not in blob, word
    for row in rows:
        action = str(row.get("suggested_human_action") or "")
        assert "変える" not in action or "人" in action
        assert "自動" not in action or "なし" in json.dumps(
            row, ensure_ascii=False) or "自走" in action


def test_decision_records_only_and_requires_fields(tmp_path):
    from app import autopilot as auto
    from app import outcome_ledger as ledger
    from app import shadow_mode as shadow
    manager, _ = _make_manager(tmp_path, tag="dec")
    rows = ledger.suggest(manager)
    if not rows:
        # 提案が無い場合でも採否の必須検査は行う。
        target = "suggestion-for-validation-only"
    else:
        target = str(rows[0]["id"])
    before_kill = ledger._read_shadow_count(manager)
    before_auto = auto.get_config(manager, "dummy")
    # actor・理由なしは拒否。
    assert ledger.record_decision(manager, target, "adopted", "", "理由あり")["ok"] is False
    assert ledger.record_decision(manager, target, "adopted", "tester", "")["ok"] is False
    assert ledger.record_decision(manager, target, "unknown-decision",
                                  "tester", "理由あり")["ok"] is False
    assert ledger.record_decision(manager, "", "adopted",
                                  "tester", "理由あり")["ok"] is False
    saved = ledger.record_decision(manager, target, "adopted", "tester",
                                   "人が確認したため採用する")
    assert saved["ok"] is True
    decision = saved["decision"]
    assert decision["suggestion_id"] == target
    assert decision["decision"] == "adopted"
    assert decision["actor"] == "tester"
    # 記録だけで何も変わらない。
    assert auto.get_config(manager, "dummy") == before_auto
    assert ledger._read_shadow_count(manager) == before_kill
    assert shadow.THRESHOLDS["max_dangerous"] == 0
    # 履歴は追記のみ。
    second = ledger.record_decision(manager, target, "rejected", "tester2",
                                    "見直したため却下する")
    assert second["ok"] is True
    history = ledger.list_decisions(manager, target)
    assert len(history) >= 2
    assert history[0]["decision"] == "rejected"


def test_decision_write_failure_returns_failure(tmp_path):
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="fail")
    ledger.FAIL_INJECT = {"stage": "decision_write_fail"}
    try:
        out = ledger.record_decision(manager, "some-id", "adopted",
                                     "tester", "理由あり")
    finally:
        ledger.FAIL_INJECT = None
    assert out["ok"] is False
    assert "error_type" in out


def test_purge_anonymizes_project_id(tmp_path):
    from app import outcome_ledger as ledger
    manager, pid = _make_manager(tmp_path, tag="purge")
    first = ledger.record_snapshot(manager, "tester")
    assert first["ok"] is True
    out = ledger.purge_project_ledger(manager.memory.path, pid)
    assert set(out.keys()) == {"ledger_snapshots",
                               "ledger_suggestion_decisions"}
    path = ledger.ledger_db_path(manager)
    blob = path.read_bytes().decode("utf-8", errors="ignore")
    assert pid not in blob
