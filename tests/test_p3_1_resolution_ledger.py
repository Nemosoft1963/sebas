"""P3-1: 解決確認台帳。偽の計画・偽の指摘で行う。外部AI・実通信は使わない。

意味の妥当性(パッチが指摘を実際に解決するか)は未検査。retainedは解決済みではない。
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import app.plan_repair_loop as loop
from app.goal_review import ReviewStore, plan_snapshot
from app.structured_planning import compile_plan, compile_task


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p3-1")["id"]
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
    mem.replace_plan(pid, "p3-1 plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _issue(iid, remedy="検証記録を工程に添える"):
    row = {"id": iid, "criterion": "SC01",
           "text": "公開前に承認し、送信前の検証順序を明記する"}
    if remedy is not None:
        row["remedy"] = remedy
    return row


def _generic_issue(iid):
    return {"id": iid, "criterion": "SC01",
            "text": "公開前に承認し、送信前の検証順序を明記する",
            "remedy": "結果を外部へ送信する"}


def _approve(iid):
    return {"issue_id": iid, "approved": True, "reviewer": "human", "reason": "内容を確認した"}


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


def _drive_to_rediff(mgr, pid, run_id, monkeypatch=None):
    _run_advance(mgr, pid, run_id, "structure_checked")
    _run_advance(mgr, pid, run_id, "coverage_checked")
    if monkeypatch is not None:
        monkeypatch.setattr(loop, "_do_external_reviewed", _fake_external)
    else:
        orig = loop._do_external_reviewed
        loop._do_external_reviewed = _fake_external
        try:
            _run_advance(mgr, pid, run_id, "external_reviewed",
                         payload={"public_summary": "公開用の目標・達成条件・工程の要約です。" * 3,
                                  "safe_to_send": True})
        finally:
            loop._do_external_reviewed = orig
        _run_advance(mgr, pid, run_id, "rediff_evaluated")
        return
    _run_advance(mgr, pid, run_id, "external_reviewed",
                 payload={"public_summary": "公開用の目標・達成条件・工程の要約です。" * 3,
                          "safe_to_send": True})
    _run_advance(mgr, pid, run_id, "rediff_evaluated")


def test_ledger_retained_issue_remedy(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1")])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    ledger = loop.resolution_ledger(mission, stored)
    assert len(ledger["rows"]) == 1
    row = ledger["rows"][0]
    assert row["issue_id"] == "r1"
    assert row["status"] == "retained"
    assert row["patch_basis"] == "issue_remedy"
    assert row["codes"] == []
    assert ledger["all_retained"] is True


def test_ledger_generic_only(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_generic_issue("g1")])
    assert run["candidates"][0]["patch_basis"] == "generic"
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("g1")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    ledger = loop.resolution_ledger(mission, stored)
    assert ledger["rows"][0]["status"] == "retained"
    assert ledger["rows"][0]["codes"] == ["generic_only"]
    assert ledger["all_retained"] is True


def test_ledger_not_retained_after_removal_and_task_missing(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1")])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    stored = loop.get_run(mgr, pid, run["id"])
    cand = stored["candidates"][0]
    before_desc = cand["patch"]["before"]["description"]
    # 追記を消す別の編集を模擬する
    mission = mem.get_mission(pid)
    tasks = []
    for t in mission["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand["task_key"]:
            row = dict(row)
            row["description"] = before_desc
        tasks.append(row)
    tampered = copy.deepcopy(mission)
    tampered["tasks"] = tasks
    ledger = loop.resolution_ledger(tampered, stored)
    assert ledger["rows"][0]["status"] == "not_retained"
    assert ledger["rows"][0]["codes"] == ["remedy_not_in_plan"]
    assert ledger["all_retained"] is False
    # generic 版
    mgr2, mem2, pid2 = _manager(tmp_path / "g")
    _plan(mgr2, mem2, pid2)
    _contract(mgr2, pid2)
    run2 = loop.start(mgr2, pid2, "human-a", [_generic_issue("g1")])
    _run_advance(mgr2, pid2, run2["id"], "patched",
                 payload={"approvals": [_approve("g1")]})
    stored2 = loop.get_run(mgr2, pid2, run2["id"])
    cand2 = stored2["candidates"][0]
    mission2 = mem2.get_mission(pid2)
    tasks2 = []
    for t in mission2["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand2["task_key"]:
            row = dict(row)
            row["description"] = cand2["patch"]["before"]["description"]
        tasks2.append(row)
    tampered2 = copy.deepcopy(mission2)
    tampered2["tasks"] = tasks2
    ledger2 = loop.resolution_ledger(tampered2, stored2)
    assert ledger2["rows"][0]["status"] == "not_retained"
    assert ledger2["rows"][0]["codes"] == ["patch_not_in_plan"]
    assert ledger2["all_retained"] is False
    # 工程が現行に無い
    missing = copy.deepcopy(mission)
    missing["tasks"] = [t for t in missing["tasks"]
                        if str(t.get("task_key") or "") != cand["task_key"]]
    ledger3 = loop.resolution_ledger(missing, stored)
    assert ledger3["rows"][0]["status"] == "not_retained"
    assert ledger3["rows"][0]["codes"] == ["task_missing"]
    # 承認なしは空で True
    run0 = loop.start(mgr2, pid2, "human-a", [_issue("z9")])
    stored0 = loop.get_run(mgr2, pid2, run0["id"])
    assert stored0["applied"] == []
    ledger0 = loop.resolution_ledger(mem2.get_mission(pid2), stored0)
    assert ledger0 == {"rows": [], "all_retained": True, "codes": []}


def test_two_issues_same_task_both_applied(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = {"id": "a2", "criterion": "SC01",
          "text": "成果物を検証後に公開する依存順序を追加する",
          "remedy": "追加の確認手順を工程に残す"}
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    assert run["candidates"][0]["task_key"] == run["candidates"][1]["task_key"] == "SC01"
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    ledger = loop.resolution_ledger(mission, stored)
    assert len(ledger["rows"]) == 2
    # P3-2: 同じ工程の2件は両方とも適用され retained になる。
    by_id = {r["issue_id"]: r for r in ledger["rows"]}
    assert by_id["a1"]["status"] == "retained"
    assert by_id["a2"]["status"] == "retained"
    assert ledger["all_retained"] is True
    reasons = loop._evaluate_pass(mgr, pid, stored)
    assert not any("PATCH_NOT_RETAINED" in r for r in reasons), reasons
    _drive_to_rediff(mgr, pid, run["id"], monkeypatch)
    # 台帳の検出力の確認: 2件目の追記だけを計画の説明から消す(別の編集を模擬する)と、
    # 2件目が not_retained(remedy_not_in_plan)になり passed が止まる。
    stored2 = loop.get_run(mgr, pid, run["id"])
    cand2 = next(c for c in stored2["candidates"] if c["issue_id"] == "a2")
    from app.local_patch_preview import REMEDY_TAG as _TAG

    mission2 = mem.get_mission(pid)
    remedy_line2 = ""
    for line in str(cand2["patch"]["after"]["description"] or "").splitlines():
        if line.startswith(_TAG):
            remedy_line2 = line
    assert remedy_line2
    tasks = []
    for t in mission2["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand2["task_key"]:
            row = dict(row)
            row["description"] = str(row.get("description") or "").replace(remedy_line2, "")
        tasks.append(row)
    tampered = copy.deepcopy(mission2)
    tampered["tasks"] = tasks
    tampered_ledger = loop.resolution_ledger(tampered, stored2)
    tampered_by_id = {r["issue_id"]: r for r in tampered_ledger["rows"]}
    assert tampered_by_id["a2"]["status"] == "not_retained"
    assert tampered_by_id["a2"]["codes"] == ["remedy_not_in_plan"]
    assert tampered_ledger["all_retained"] is False
    fresh = copy.deepcopy(stored2)
    # 署名を変えずに tampered を直接渡すのではなく、現行計画を書き換えて再計算する。
    mem.replace_plan(pid, mission2.get("plan_summary") or "", tasks,
                      expected_version=mission2.get("plan_version"))
    new_sig = plan_snapshot(mgr, pid)[1]
    fresh = loop.get_run(mgr, pid, run["id"])
    fresh["patched_signature"] = new_sig
    ReviewStore(mem.path).put(pid, "repair_run", run["id"], fresh)
    reasons = loop._evaluate_pass(mgr, pid, loop.get_run(mgr, pid, run["id"]))
    assert any("PATCH_NOT_RETAINED" in r for r in reasons), reasons
    assert any("a2" in r for r in reasons if "PATCH_NOT_RETAINED" in r)


def test_ledger_error_and_runtime_propagates(tmp_path):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1")])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    broken = copy.deepcopy(stored)
    broken["candidates"] = ["broken"]
    ledger = loop.resolution_ledger(mission, broken)
    assert ledger["all_retained"] is False
    assert ledger["codes"] == ["ledger_error"]
    assert ledger["details"] in ("AttributeError", "TypeError", "ValueError", "KeyError")

    class _Boom(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        loop.resolution_ledger(_Boom(mission), stored)


def test_rediff_saves_ledger_and_passed_recomputes(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1")])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    _drive_to_rediff(mgr, pid, run["id"], monkeypatch)
    stored = loop.get_run(mgr, pid, run["id"])
    saved = (stored.get("evidence") or {}).get("rediff_evaluated") or {}
    assert "ledger" in saved
    assert saved["ledger"]["all_retained"] is True
    assert saved["ledger"]["rows"][0]["status"] == "retained"
    # 台帳OKで他の条件も満たすときは通る
    out = _run_advance(mgr, pid, run["id"], "passed")
    assert out["state"] == "passed"

    # 別ランでも両方適用される(P3-2)。台帳の検出力の実演は rediff 前段の
    # not_retained(追記除去の模擬)で行う。
    mgr2, mem2, pid2 = _manager(tmp_path / "b")
    _plan(mgr2, mem2, pid2)
    _contract(mgr2, pid2)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = {"id": "a2", "criterion": "SC01",
          "text": "成果物を検証後に公開する依存順序を追加する",
          "remedy": "追加の確認手順を工程に残す"}
    run2 = loop.start(mgr2, pid2, "human-a", [i1, i2])
    _run_advance(mgr2, pid2, run2["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    stored2_pre = loop.get_run(mgr2, pid2, run2["id"])
    ledger2_pre = loop.resolution_ledger(mem2.get_mission(pid2), stored2_pre)
    assert ledger2_pre["all_retained"] is True
    # 2件目の追記だけを計画から消す(別の編集を模擬する)と not_retained で止まる
    cand2 = next(c for c in stored2_pre["candidates"] if c["issue_id"] == "a2")
    from app.local_patch_preview import REMEDY_TAG as _TAG2

    remedy_line = ""
    for line in str(cand2["patch"]["after"]["description"] or "").splitlines():
        if line.startswith(_TAG2):
            remedy_line = line
    assert remedy_line
    mission2 = mem2.get_mission(pid2)
    tasks = []
    for t in mission2["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand2["task_key"]:
            row = dict(row)
            row["description"] = str(row.get("description") or "").replace(remedy_line, "")
        tasks.append(row)
    mem2.replace_plan(pid2, mission2.get("plan_summary") or "", tasks,
                      expected_version=mission2.get("plan_version"))
    new_sig2 = plan_snapshot(mgr2, pid2)[1]
    stored2_pre["patched_signature"] = new_sig2
    ReviewStore(mem2.path).put(pid2, "repair_run", run2["id"], stored2_pre)
    _drive_to_rediff(mgr2, pid2, run2["id"], monkeypatch)
    with pytest.raises(ValueError, match="REPAIR_NOT_PASSED.*PATCH_NOT_RETAINED"):
        _run_advance(mgr2, pid2, run2["id"], "passed")

    # 再計算の証明: 保存値はTrueのまま、現行を消すと止まる
    mgr3, mem3, pid3 = _manager(tmp_path / "c")
    _plan(mgr3, mem3, pid3)
    _contract(mgr3, pid3)
    run3 = loop.start(mgr3, pid3, "human-a", [_issue("r1")])
    _run_advance(mgr3, pid3, run3["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    _drive_to_rediff(mgr3, pid3, run3["id"], monkeypatch)
    stored3 = loop.get_run(mgr3, pid3, run3["id"])
    assert stored3["evidence"]["rediff_evaluated"]["ledger"]["all_retained"] is True
    cand = stored3["candidates"][0]
    mission3 = mem3.get_mission(pid3)
    tasks = []
    for t in mission3["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand["task_key"]:
            row = dict(row)
            row["description"] = cand["patch"]["before"]["description"]
        tasks.append(row)
    mem3.replace_plan(pid3, mission3.get("plan_summary") or "", tasks,
                      expected_version=mission3.get("plan_version"))
    new_sig = plan_snapshot(mgr3, pid3)[1]
    stored3 = loop.get_run(mgr3, pid3, run3["id"])
    stored3["patched_signature"] = new_sig
    ReviewStore(mem3.path).put(pid3, "repair_run", run3["id"], stored3)
    reasons = loop._evaluate_pass(mgr3, pid3, loop.get_run(mgr3, pid3, run3["id"]))
    assert any("PATCH_NOT_RETAINED" in r for r in reasons), reasons
    with pytest.raises(ValueError, match="PATCH_NOT_RETAINED"):
        _run_advance(mgr3, pid3, run3["id"], "passed")


def test_repair_cards_resolution_texts(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1"), _generic_issue("g1")])
    cards0 = loop.repair_cards(mgr, pid)["cards"]
    by_id0 = {c["issue_id"]: c for c in cards0}
    assert by_id0["r1"]["解決確認"] == "未適用"
    assert by_id0["r1"]["再評価結果"] != ""
    before_result = by_id0["r1"]["再評価結果"]
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1"), _approve("g1")]})
    _drive_to_rediff(mgr, pid, run["id"], monkeypatch)
    cards = loop.repair_cards(mgr, pid)["cards"]
    by_id = {c["issue_id"]: c for c in cards if c["run_id"] == run["id"]}
    assert by_id["r1"]["解決確認"] == "指摘の修正案が計画に反映済み(意味の妥当性は未検査)"
    assert "定型文のみ対応" in by_id["g1"]["解決確認"]
    assert "人間確認が必要" in by_id["g1"]["解決確認"]
    # 未解決(消した)版: 署名が変わるため cards フィルタから外れる。台帳で直接確認する
    stored = loop.get_run(mgr, pid, run["id"])
    cand = next(c for c in stored["candidates"] if c["issue_id"] == "r1")
    mission = mem.get_mission(pid)
    tasks = []
    for t in mission["tasks"]:
        row = dict(t)
        if str(row.get("task_key") or "") == cand["task_key"]:
            row = dict(row)
            row["description"] = cand["patch"]["before"]["description"]
        tasks.append(row)
    mem.replace_plan(pid, mission.get("plan_summary") or "", tasks,
                      expected_version=mission.get("plan_version"))
    tampered = copy.deepcopy(loop.get_run(mgr, pid, run["id"]))
    tampered_mission = mem.get_mission(pid)
    not_retained = loop.resolution_ledger(tampered_mission, tampered)
    assert not_retained["rows"][0]["status"] == "not_retained"
    assert loop._ledger_resolution_text(not_retained, "r1") == "適用したパッチが計画に残っていない(未解決)"
    # ledger_error のとき確認不可: run.applied を読む破損で示す
    mgr_e, mem_e, pid_e = _manager(tmp_path / "e")
    _plan(mgr_e, mem_e, pid_e)
    _contract(mgr_e, pid_e)
    run_e = loop.start(mgr_e, pid_e, "human-a", [_issue("r1")])
    _run_advance(mgr_e, pid_e, run_e["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    broken = loop.get_run(mgr_e, pid_e, run_e["id"])
    broken["applied"] = "broken"
    ReviewStore(mem_e.path).put(pid_e, "repair_run", run_e["id"], broken)
    cards3 = loop.repair_cards(mgr_e, pid_e)["cards"]
    avail3 = {c["issue_id"]: c for c in cards3 if c["run_id"] == run_e["id"]}
    assert avail3["r1"]["解決確認"] == "確認不可(未解決として扱う)"
    # 既存の再評価結果は既存の固定文のいずれかであり、台帳追加で削られていない
    assert by_id["r1"]["再評価結果"] in (
        "対応づけ済み・再検証待ち", "内容レビュー待ち", "レビュー不可のため承認不可",
        "回答待ち（未解決）", "未結合・要実装のため未解決", "再評価で未解決")
    assert "再評価結果" in by_id0["r1"]


def test_ledger_no_writes_no_external(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [_issue("r1")])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("r1")]})
    stored = loop.get_run(mgr, pid, run["id"])
    mission = mem.get_mission(pid)
    before_mission = copy.deepcopy(mission)
    before_run = copy.deepcopy(stored)
    before_runs = sorted([sig for sig, _p in ReviewStore(mem.path).list(pid, "repair_run")])

    def _boom(*args, **kwargs):
        raise AssertionError("external call is not allowed")

    monkeypatch.setattr("app.goal_review.review_plan", _boom)
    ledger = loop.resolution_ledger(mission, stored)
    assert ledger["all_retained"] is True
    assert mem.get_mission(pid) == before_mission
    assert loop.get_run(mgr, pid, run["id"]) == before_run
    after_runs = sorted([sig for sig, _p in ReviewStore(mem.path).list(pid, "repair_run")])
    assert after_runs == before_runs


def test_old_code_without_ledger_would_pass(tmp_path, monkeypatch):
    """P3-2で更新: 同一工程2件は両方適用され台帳は True になる。

    台帳の検出力の実演は ledger_error の fail-closed で残す。修正前相当
    (無条件 all_retained=True)は検出が出ないが、新コードの実台帳は True で、
    台帳取得が壊れれば PATCH_NOT_RETAINED で止まる。
    monkeypatch のため終了後は自動で元に戻る。戻ったことを確認する。
    """
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    i1 = _issue("a1", "検証記録を工程に添える")
    i2 = {"id": "a2", "criterion": "SC01",
          "text": "成果物を検証後に公開する依存順序を追加する",
          "remedy": "追加の確認手順を工程に残す"}
    run = loop.start(mgr, pid, "human-a", [i1, i2])
    _run_advance(mgr, pid, run["id"], "patched",
                 payload={"approvals": [_approve("a1"), _approve("a2")]})
    _drive_to_rediff(mgr, pid, run["id"], monkeypatch)
    stored = loop.get_run(mgr, pid, run["id"])
    # P3-2 の新動作: 両方適用され、実台帳は True。
    assert loop.resolution_ledger(mem.get_mission(pid), stored)["all_retained"] is True
    real_ledger = loop.resolution_ledger
    # 実演(1): 旧相当の「台帳を見ない」全 True 差し替えでは他の検出は出ない。
    monkeypatch.setattr(loop, "resolution_ledger",
                        lambda *a, **k: {"rows": [], "all_retained": True, "codes": []})
    old_reasons = loop._evaluate_pass(mgr, pid, loop.get_run(mgr, pid, run["id"]))
    old_not_retained = [r for r in old_reasons if "PATCH_NOT_RETAINED" in r]
    monkeypatch.undo()
    # 実演(2): 台帳取得が ledger_error で壊れれば新コードは PATCH_NOT_RETAINED で止まる。
    monkeypatch.setattr(loop, "resolution_ledger",
                        lambda *a, **k: {"rows": [], "all_retained": False, "codes": ["ledger_error"],
                                         "details": "test"})
    err_reasons = loop._evaluate_pass(mgr, pid, loop.get_run(mgr, pid, run["id"]))
    assert any("PATCH_NOT_RETAINED" in r for r in err_reasons), err_reasons
    monkeypatch.undo()
    assert loop.resolution_ledger is real_ledger
    # 新コードの実台帳は True のまま(両方適用)。
    new_reasons = loop._evaluate_pass(mgr, pid, loop.get_run(mgr, pid, run["id"]))
    assert not any("PATCH_NOT_RETAINED" in r for r in new_reasons), new_reasons
    assert old_not_retained == []
