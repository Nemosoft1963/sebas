"""P3-2: 同一工程の複数パッチ適用。偽の計画・偽の指摘で行う。外部AI・実通信は使わない。

構造上の適用のみを検査する。意味の妥当性(パッチが指摘を実際に解決するか)は未検査。
"""
import asyncio
import copy

import pytest

import app.plan_repair_loop as loop
from app.goal_review import ReviewStore, plan_snapshot
from app.structured_planning import compile_plan, compile_task


class _M:
    pass


def _manager(tmp_path):
    from types import SimpleNamespace
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p3-2")["id"]
    mem.save_mission(
        pid,
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
        "",
        True,
        ["chatgpt"],
    )
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = SimpleNamespace(
        memory=mem,
        workspace=ws,
        planning_projects=set(),
        llm=None,
        provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
        plan_review_runner=None,
    )
    return mgr, mem, pid


def _plan(mgr, mem, pid):
    criteria = ["匿名リードの獲得", "匿名案件の検証"]
    t1 = compile_task(
        1, criteria[0],
        {"title": "匿名施策", "scope": "匿名リードの獲得手順を整理する",
         "headings": ["現状確認", "手順"], "depends_on": []}, [],
    )
    t2 = compile_task(
        2, criteria[1],
        {"title": "匿名検証", "scope": "匿名案件の検証手順を整理する",
         "headings": ["現状確認", "検証"], "depends_on": ["SC01"]}, [],
    )
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p3-2 plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _issue(iid, remedy, criterion="SC01", text="公開前に承認し、送信前の検証順序を明記する"):
    row = {"id": iid, "criterion": criterion, "text": text}
    if remedy is not None:
        row["remedy"] = remedy
    return row


def _approve(iid, reviewer="human", reason="内容を確認した"):
    return {"issue_id": iid, "approved": True, "reviewer": reviewer, "reason": reason}


def _run_advance(mgr, pid, run_id, target, actor="human-a", payload=None):
    return asyncio.new_event_loop().run_until_complete(
        loop.advance(mgr, pid, run_id, target, actor, payload or {}))


async def _fake_external(manager, project_id, runrow, payload, actor):
    sig = plan_snapshot(manager, project_id)[1]
    ReviewStore(manager.memory.path).put(project_id, "plan", sig, {
        "status": "passed", "reviews": [{"provider": "chatgpt", "status": "pass", "issues": []}],
        "packet": {}, "success_count": 1, "required_count": 1})
    runrow["external_calls"] = int(runrow.get("external_calls") or 0) + 1
    evidence = dict(runrow.get("evidence") or {})
    evidence["external_reviewed"] = {
        "actor": actor, "signature": sig,
        "status": "passed", "outcome": "content_pass",
        "success_count": 1, "required_count": 1, "public_summary_chars": 42,
        "sent_private_originals": False, "connection_error": False,
    }
    runrow["evidence"] = evidence
    return runrow


def _drive_to_rediff(mgr, pid, run_id, monkeypatch):
    _run_advance(mgr, pid, run_id, "structure_checked")
    _run_advance(mgr, pid, run_id, "coverage_checked")
    monkeypatch.setattr(loop, "_do_external_reviewed", _fake_external)
    _run_advance(mgr, pid, run_id, "external_reviewed",
                 payload={"public_summary": "公開用の目標・達成条件・工程の要約です。" * 3,
                          "safe_to_send": True})
    _run_advance(mgr, pid, run_id, "rediff_evaluated")


def _desc_of(mission, task_key):
    for t in mission.get("tasks") or []:
        if str(t.get("task_key") or "") == task_key:
            return str(t.get("description") or "")
    raise AssertionError(f"task missing: {task_key}")


def test_two_issues_same_task_both_applied(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    before = _desc_of(mem.get_mission(pid), "SC01")
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "追加の確認手順を工程に残す",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    assert run["candidates"][0]["task_key"] == run["candidates"][1]["task_key"] == "SC01"
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    new_desc = _desc_of(mission, "SC01")
    assert new_desc.startswith(before.rstrip())
    assert "指摘対応: 検証記録を工程に添える" in new_desc
    assert "指摘対応: 追加の確認手順を工程に残す" in new_desc
    assert new_desc.count("指摘対応: 検証記録を工程に添える") == 1
    assert new_desc.count("指摘対応: 追加の確認手順を工程に残す") == 1
    assert new_desc.count("局所修復候補:") == 1
    ledger = loop.resolution_ledger(mission, stored)
    assert len(ledger["rows"]) == 2
    assert {r["issue_id"]: r["status"] for r in ledger["rows"]} == {"a1": "retained", "a2": "retained"}
    assert ledger["all_retained"] is True


def test_same_remedy_and_no_remedy_no_duplicate(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "検証記録を工程に添える",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    new_desc = _desc_of(mem.get_mission(pid), "SC01")
    assert new_desc.count("指摘対応: 検証記録を工程に添える") == 1
    assert new_desc.count("局所修復候補:") == 1
    # 両方が修正案なし(同じ定型文)の場合
    mgr2, mem2, pid2 = _manager(tmp_path / "g")
    _plan(mgr2, mem2, pid2)
    _contract(mgr2, pid2)
    g1 = {"id": "g1", "criterion": "SC01",
          "text": "公開前に承認し、送信前の検証順序を明記する",
          "remedy": "結果を外部へ送信する"}
    g2 = {"id": "g2", "criterion": "SC01",
          "text": "成果物を検証後に公開する依存順序を追加する",
          "remedy": "結果を外部へ送信する"}
    run2 = loop.start(mgr2, pid2, "human-a", [g1, g2])
    assert run2["candidates"][0]["patch_basis"] == "generic"
    assert run2["candidates"][1]["patch_basis"] == "generic"
    _run_advance(mgr2, pid2, run2["id"], "patched",
                 payload={"approvals": [_approve("g1"), _approve("g2")]})
    new_desc2 = _desc_of(mem2.get_mission(pid2), "SC01")
    assert new_desc2.count("局所修復候補:") == 1


def test_single_apply_identical_and_separate_tasks(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "追加の確認手順を工程に残す",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    stored_before = loop.get_run(mgr, pid, run["id"])
    cand = next(c for c in stored_before["candidates"] if c["issue_id"] == "a1")
    expected = cand["patch"]["after"]["description"]
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1")]})
    new_desc = _desc_of(mem.get_mission(pid), "SC01")
    assert new_desc == expected
    # 別の工程への2件承認
    mgr2, mem2, pid2 = _manager(tmp_path / "s")
    _plan(mgr2, mem2, pid2)
    _contract(mgr2, pid2)
    s1 = _issue("s1", "検証記録を工程に添える")
    s2 = _issue("s2", "確認記録を工程に残す", criterion="SC02",
                text="検証の記録を工程に残す")
    run2 = loop.start(mgr2, pid2, "human-a", [s1, s2])
    keys = {c["issue_id"]: c["task_key"] for c in run2["candidates"]}
    assert keys["s1"] == "SC01"
    assert keys["s2"] == "SC02"
    _run_advance(mgr2, pid2, run2["id"], "patched",
                 payload={"approvals": [_approve("s1"), _approve("s2")]})
    stored2 = loop.get_run(mgr2, pid2, run2["id"])
    assert len(stored2["applied"]) == 2
    m2 = mem2.get_mission(pid2)
    assert "指摘対応: 検証記録を工程に添える" in _desc_of(m2, "SC01")
    assert "指摘対応: 確認記録を工程に残す" in _desc_of(m2, "SC02")


def test_per_issue_reviewers(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "追加の確認手順を工程に残す",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    _run_advance(mgr, pid, run["id"], "patched", payload={"approvals": [
        {"issue_id": "a1", "approved": True, "reviewer": "human-a", "reason": "理由A"},
        {"issue_id": "a2", "approved": True, "reviewer": "human-b", "reason": "理由B"},
    ]})
    stored = loop.get_run(mgr, pid, run["id"])
    assert len(stored["applied"]) == 1
    entry = stored["applied"][0]
    assert entry["task_key"] == "SC01"
    assert entry["issue_ids"] == ["a1", "a2"]
    assert entry["reviewer"] == "human-a"
    assert entry["reason"] == "理由A"
    assert entry["per_issue"] == {
        "a1": {"reviewer": "human-a", "reason": "理由A"},
        "a2": {"reviewer": "human-b", "reason": "理由B"},
    }


def test_merge_failed_fail_closed(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "追加の確認手順を工程に残す",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    stored = loop.get_run(mgr, pid, run["id"])
    for cand in stored["candidates"]:
        if cand["issue_id"] == "a2":
            cand["patch"]["after"]["description"] = "全く別の説明(接頭辞を持たない)"
    ReviewStore(mem.path).put(pid, "repair_run", run["id"], stored)
    monkeypatch.setattr(loop, "check_candidate",
                        lambda *a, **k: {"passed": True, "codes": [], "details": {}})
    before_mission = mem.get_mission(pid)
    before_version = before_mission.get("plan_version")
    before_tasks = copy.deepcopy(before_mission.get("tasks"))
    with pytest.raises(ValueError, match="PATCH_MERGE_FAILED.*a2"):
        _run_advance(mgr, pid, run["id"], "patched",
                     payload={"approvals": [_approve("a1"), _approve("a2")]})
    after_mission = mem.get_mission(pid)
    assert after_mission.get("plan_version") == before_version
    assert after_mission.get("tasks") == before_tasks


def test_advance_to_passed_with_two_issues(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = _issue("a2", "追加の確認手順を工程に残す",
                text="成果物を検証後に公開する依存順序を追加する")
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    _drive_to_rediff(mgr, pid, run["id"], monkeypatch)
    stored = loop.get_run(mgr, pid, run["id"])
    saved = (stored.get("evidence") or {}).get("rediff_evaluated") or {}
    assert saved["ledger"]["all_retained"] is True
    out = _run_advance(mgr, pid, run["id"], "passed")
    assert out["state"] == "passed"
