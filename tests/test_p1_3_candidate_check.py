"""P1-3: 候補パッチの独立検査(check_candidate)。偽の計画・偽の指摘で行う。

- 外部AI・実通信は使わない。意味の妥当性(指摘内容を実際に解決するか)は未検査。
- check.passed は構造上の安全確認だけであり、解決済みの表示・記録に使わない。
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import app.plan_repair_loop as loop
from app.goal_review import ReviewStore, plan_snapshot
from app.structured_planning import compile_plan, compile_task, contract_of


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p1-3")["id"]
    mem.save_mission(
        pid,
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
        "",
        True,
        ["chatgpt"],
    )
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(
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
    mem.replace_plan(pid, "p1-3 plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _unit_fixture():
    """実在の compile_* で作った偽の計画・契約・正常候補を返す。"""
    c1 = "匿名リードの獲得手順を整理する"
    c2 = "匿名案件の検証手順を整理する"
    t1 = compile_task(
        1, c1,
        {"title": "匿名施策", "scope": "匿名リードの獲得手順を整理する",
         "headings": ["現状確認", "手順"], "depends_on": []}, [],
    )
    t2 = compile_task(
        2, c2,
        {"title": "匿名検証", "scope": "匿名案件の検証手順を整理する",
         "headings": ["現状確認", "検証"], "depends_on": ["SC01"]}, [],
    )
    compiled = compile_plan([c1, c2], [t1, t2], goal="偽の目標")
    mission = {"goal": "偽の目標", "success_criteria": "偽の成功条件",
               "tasks": compiled["tasks"], "plan_version": 1}
    contract = {"criteria": [{"criterion_id": "SC01", "statement": c1},
                              {"criterion_id": "SC02", "statement": c2}],
                "content_hash": "pseudo-hash", "extracted_criteria": [c1, c2]}
    task = next(t for t in mission["tasks"] if t.get("task_key") == "SC01")
    before = {"description": str(task.get("description") or ""),
              "depends_on": list(task.get("depends_on") or []),
              "contract": copy.deepcopy(contract_of(task) or {})}
    after = copy.deepcopy(before)
    after["description"] = (after["description"].rstrip()
                            + "\n\n局所修復候補: 偽の追記。")
    cand = {"issue_id": "pseudo-i1", "task_key": "SC01",
            "classification": "safe_plan_patch", "bound": True,
            "patch": {"task_key": "SC01", "before": before, "after": after}}
    return mission, contract, cand


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def test_normal_patch_passes_and_no_patch_skipped():
    mission, contract, cand = _unit_fixture()
    good = loop.check_candidate(mission, contract, cand)
    assert good["passed"] is True, good
    assert good["codes"] == []
    fact = {"issue_id": "pseudo-fact", "task_key": "SC01",
            "classification": "business_fact", "bound": True, "patch": None}
    skipped = loop.check_candidate(mission, contract, fact)
    assert skipped["passed"] is None
    assert skipped["codes"] == []
    assert skipped["details"] == {"skipped": "no_patch"}


def test_stale_before_on_changed_or_missing_task():
    mission, contract, cand = _unit_fixture()
    changed = copy.deepcopy(cand)
    changed["patch"]["before"]["description"] += "（計画変更のつもり）"
    out = loop.check_candidate(mission, contract, changed)
    assert out["passed"] is False
    assert "stale_before" in out["codes"]
    missing = copy.deepcopy(cand)
    missing["task_key"] = "SC99"
    missing["patch"]["task_key"] = "SC99"
    out2 = loop.check_candidate(mission, contract, missing)
    assert out2["passed"] is False
    assert "stale_before" in out2["codes"]


def test_unexpected_change_and_no_effect():
    mission, contract, cand = _unit_fixture()
    dep = copy.deepcopy(cand)
    dep["patch"]["after"]["depends_on"] = ["SC02"]
    out = loop.check_candidate(mission, contract, dep)
    assert out["passed"] is False
    assert "unexpected_change" in out["codes"]
    trimmed = copy.deepcopy(cand)
    before_desc = trimmed["patch"]["before"]["description"]
    trimmed["patch"]["after"]["description"] = before_desc[len(before_desc) // 2:]
    out2 = loop.check_candidate(mission, contract, trimmed)
    assert out2["passed"] is False
    assert "unexpected_change" in out2["codes"]
    rewritten = copy.deepcopy(cand)
    rewritten["patch"]["after"]["description"] = "書き換えた説明"
    out3 = loop.check_candidate(mission, contract, rewritten)
    assert "unexpected_change" in out3["codes"]
    same = copy.deepcopy(cand)
    same["patch"]["after"] = copy.deepcopy(same["patch"]["before"])
    out4 = loop.check_candidate(mission, contract, same)
    assert out4["passed"] is False
    assert "no_effect" in out4["codes"]


def test_regression_codes_and_no_blame_when_already_failing(monkeypatch):
    mission, contract, cand = _unit_fixture()

    def fake_struct(m, _c):
        descs = [str(t.get("description") or "") for t in (m.get("tasks") or [])
                 if isinstance(t, dict)]
        if any("REGRESS-STRUCT" in d for d in descs):
            return {"passed": False, "issues": ["SC01: broken"]}
        return {"passed": True, "issues": []}

    def fake_cov_ok(_m, _c):
        return {"coverage": {"passed": True, "issues": []},
                "retention": {"passed": True, "missing_topics": []}}

    monkeypatch.setattr(loop, "_run_structure_check", fake_struct)
    monkeypatch.setattr(loop, "_run_coverage_check", fake_cov_ok)
    broken = copy.deepcopy(cand)
    broken["patch"]["after"]["description"] += "\nREGRESS-STRUCT"
    out = loop.check_candidate(mission, contract, broken)
    assert out["passed"] is False
    assert "regression_structure" in out["codes"]

    def fake_struct_always_bad(_m, _c):
        return {"passed": False, "issues": ["SC01: old problem"]}

    def fake_cov_grows(m, _c):
        descs = [str(t.get("description") or "") for t in (m.get("tasks") or [])
                 if isinstance(t, dict)]
        if any("REGRESS-COV" in d for d in descs):
            return {"coverage": {"passed": False, "issues": ["SC01: old", "SC01: new"]},
                    "retention": {"passed": True, "missing_topics": []}}
        return {"coverage": {"passed": False, "issues": ["SC01: old"]},
                "retention": {"passed": False, "missing_topics": ["old-topic"]}}

    monkeypatch.setattr(loop, "_run_structure_check", fake_struct_always_bad)
    monkeypatch.setattr(loop, "_run_coverage_check", fake_cov_grows)
    # 適用前から通っていない検査は、悪化しない限りコードにしない。
    calm = loop.check_candidate(mission, contract, cand)
    assert "regression_structure" not in calm["codes"]
    assert "regression_coverage" not in calm["codes"]
    assert "regression_retention" not in calm["codes"]
    # 適用前は通らず、適用後に問題が増えた場合は悪化として扱う。
    grown = copy.deepcopy(cand)
    grown["patch"]["after"]["description"] += "\nREGRESS-COV"
    out2 = loop.check_candidate(mission, contract, grown)
    assert "regression_coverage" in out2["codes"]

    def fake_cov_retention_grows(m, _c):
        descs = [str(t.get("description") or "") for t in (m.get("tasks") or [])
                 if isinstance(t, dict)]
        if any("REGRESS-RET" in d for d in descs):
            return {"coverage": {"passed": True, "issues": []},
                    "retention": {"passed": False, "missing_topics": ["old-topic", "new-topic"]}}
        return {"coverage": {"passed": True, "issues": []},
                "retention": {"passed": False, "missing_topics": ["old-topic"]}}

    monkeypatch.setattr(loop, "_run_coverage_check", fake_cov_retention_grows)
    ret = copy.deepcopy(cand)
    ret["patch"]["after"]["description"] += "\nREGRESS-RET"
    out3 = loop.check_candidate(mission, contract, ret)
    assert "regression_retention" in out3["codes"]
    calm2 = loop.check_candidate(mission, contract, cand)
    assert "regression_retention" not in calm2["codes"]


def test_check_error_fail_closed_and_unexpected_propagates(monkeypatch):
    mission, contract, cand = _unit_fixture()
    bad_shape = {"issue_id": "pseudo-bad", "task_key": "SC01", "patch": "not-a-dict"}
    out = loop.check_candidate(mission, contract, bad_shape)
    assert out["passed"] is False
    assert out["codes"] == ["check_error"]
    assert out["details"]["error"] in ("ValueError", "KeyError", "TypeError",
                                       "AttributeError", "OSError")
    out2 = loop.check_candidate(object(), contract, cand)
    assert out2["passed"] is False
    assert out2["codes"] == ["check_error"]

    def boom(_m, _c):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(loop, "_run_structure_check", boom)
    with pytest.raises(RuntimeError):
        loop.check_candidate(mission, contract, cand)


def test_start_exposes_check_without_writes_or_external_calls(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    signature = plan_snapshot(mgr, pid)[1]
    mission_before = _canon(mem.get_mission(pid))
    store_before = {kind: _canon(list(ReviewStore(mgr.memory.path).list(pid, kind)))
                    for kind in ("binding_snapshot", "feedback", "plan", "send_approval")}
    issues = [
        {"id": "i-safe", "criterion": "SC01",
         "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "i-fact", "criterion": "SC01", "text": "リードの有効条件を決定する"},
        {"id": "i-dev", "criterion": "SC99", "text": "外部API連携を新規実装する"},
    ]
    run = loop.start(mgr, pid, "human-a", issues)
    assert run["external_calls"] == 0
    assert plan_snapshot(mgr, pid)[1] == signature
    assert _canon(mem.get_mission(pid)) == mission_before
    for kind in ("binding_snapshot", "feedback", "plan", "send_approval"):
        assert _canon(list(ReviewStore(mgr.memory.path).list(pid, kind))) == store_before[kind]
    by_id = {c["issue_id"]: c for c in run["candidates"]}
    assert by_id["i-safe"]["classification"] == "safe_plan_patch"
    assert by_id["i-safe"]["check"]["passed"] is True
    assert by_id["i-safe"]["check"]["codes"] == []
    assert by_id["i-fact"]["check"]["passed"] is None
    assert by_id["i-dev"]["check"]["passed"] is None
    stored = loop.get_run(mgr, pid, run["id"])
    assert stored["candidates"][0]["check"]["passed"] is True


def test_patched_blocks_failed_check_and_applies_valid(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [
        {"id": "stale-one", "criterion": "SC01",
         "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    mission_before = mem.get_mission(pid)
    version_before = mission_before.get("plan_version")
    tasks_before = _canon(mission_before.get("tasks"))
    stored = loop.get_run(mgr, pid, run["id"])
    stored["candidates"][0]["patch"]["before"]["description"] += "（後から改ざん）"
    ReviewStore(mgr.memory.path).put(pid, "repair_run", stored["id"], stored)
    payload = {"approvals": [{"issue_id": "stale-one", "approved": True,
                              "reviewer": "human", "reason": "確認した"}]}
    with pytest.raises(ValueError, match="CANDIDATE_CHECK_FAILED"):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "human-a", payload))
    mission_after = mem.get_mission(pid)
    assert mission_after.get("plan_version") == version_before
    assert _canon(mission_after.get("tasks")) == tasks_before
    assert loop.get_run(mgr, pid, run["id"])["state"] == "proposed"

    run2 = loop.start(mgr, pid, "human-a", [
        {"id": "good-one", "criterion": "SC01",
         "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    payload2 = {"approvals": [{"issue_id": "good-one", "approved": True,
                               "reviewer": "human", "reason": "確認した"}]}
    out = asyncio.new_event_loop().run_until_complete(
        loop.advance(mgr, pid, run2["id"], "patched", "human-a", payload2))
    assert out["state"] == "patched"
    mission_new = mem.get_mission(pid)
    assert mission_new.get("plan_version") != version_before
    descs = [str(t.get("description") or "") for t in mission_new.get("tasks") or []]
    assert any("局所修復候補" in d for d in descs)


def test_without_recheck_old_code_would_apply_stale_patch(tmp_path):
    """修正前の振る舞いの再現: 再検査なしでは古い候補が適用されてしまう。

    再検査の呼出だけを一時的に外して走らせ、終わったら必ず元に戻す。
    """
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    version_before = mem.get_mission(pid).get("plan_version")
    run = loop.start(mgr, pid, "human-a", [
        {"id": "stale-old", "criterion": "SC01",
         "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    stored = loop.get_run(mgr, pid, run["id"])
    stored["candidates"][0]["patch"]["before"]["description"] += "（後から改ざん）"
    ReviewStore(mgr.memory.path).put(pid, "repair_run", stored["id"], stored)
    payload = {"approvals": [{"issue_id": "stale-old", "approved": True,
                              "reviewer": "human", "reason": "旧コード想定"}]}
    orig = loop.check_candidate
    loop.check_candidate = lambda *a, **k: {"passed": True, "codes": [], "details": {}}
    try:
        out = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "human-a", payload))
    finally:
        loop.check_candidate = orig
    assert loop.check_candidate is orig
    assert out["state"] == "patched"
    assert mem.get_mission(pid).get("plan_version") != version_before


def test_unhashable_issue_becomes_check_error_not_pass(monkeypatch):
    """検査失敗の握りつぶし防止: ハッシュ不能な issues は check_error(不合格)。"""
    mission, contract, cand = _unit_fixture()

    def fake_struct_unhashable(_m, _c):
        return {"passed": False, "issues": [{"unhashable": True}]}

    def fake_cov_ok(_m, _c):
        return {"coverage": {"passed": True, "issues": []},
                "retention": {"passed": True, "missing_topics": []}}

    monkeypatch.setattr(loop, "_run_structure_check", fake_struct_unhashable)
    monkeypatch.setattr(loop, "_run_coverage_check", fake_cov_ok)
    out = loop.check_candidate(mission, contract, cand)
    assert out["passed"] is False
    assert out["codes"] == ["check_error"]
