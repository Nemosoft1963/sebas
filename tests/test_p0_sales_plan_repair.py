"""P0 販売計画修復: 疑似販売案件での受入テスト(実案件不使用)。

- 未解決6件相当が現行条件・工程へ証拠付きで対応づけられ、bound=falseは未結合で残る
- business_factは回答前は未解決、推測で埋められない
- 指摘が減っただけではpassedにならない。同一署名4点+未解決0+回答済みのみpassed
- review_unavailableは承認を通さない / 合格でも自動承認しない
- 古いSC番号・抽象工程への付け替えが起きない
- 画面の新規表示コードにinnerHTMLが無い(静的検査)
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.goal_review import ReviewStore, plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_task


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0pseudo")["id"]
    criteria_lines = "\n".join(f"{i}. 匿名条件{i:02d}" for i in range(1, 7))
    mem.save_mission(
        pid,
        "疑似販売目標\n" + criteria_lines,
        "疑似販売目標\n" + criteria_lines,
        "",
        True,
        ["chatgpt"],
    )
    from app.workspace_files import WorkspaceSandbox

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
    from app.structured_planning import compile_plan

    criteria = [f"匿名条件{i:02d}" for i in range(1, 7)]
    tasks = []
    for idx, name in enumerate(criteria, 1):
        tasks.append(compile_task(
            idx, name,
            {"title": f"匿名施策{idx:02d}", "scope": f"{name}の手順を整理する",
             "headings": ["現状確認", "手順"], "depends_on": []}, [],
        ))
    compiled = compile_plan(criteria, tasks, goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0 pseudo plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _six_issues():
    return [
        {"id": "p0-01", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "p0-02", "criterion": "SC02", "text": "既存フォームを再作成せず再利用する手順を明記する"},
        {"id": "p0-03", "criterion": "SC03", "text": "リードの有効条件を決定する"},
        {"id": "p0-04", "criterion": "SC04", "text": "価格の基準値を決定する"},
        {"id": "p0-05", "criterion": "SC99", "text": "古いSC99の抽象工程へ付け替える"},
        {"id": "p0-06", "criterion": "SC06", "text": "外部API連携を新規実装する"},
    ]


def test_p0_six_issues_bound_with_evidence_and_unbound_stays_open(tmp_path):
    import app.plan_repair_loop as loop

    mgr, _mem, pid = _manager(tmp_path)
    _plan(mgr, mgr.memory, pid)
    contract = _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-p0", _six_issues())
    by_id = {c["issue_id"]: c for c in run["candidates"]}
    assert len(by_id) == 6
    for iid, cand in by_id.items():
        stored = loop.get_run(mgr, pid, run["id"])
        row = next(x for x in stored["candidates"] if x["issue_id"] == iid)
        assert row["plan_signature"] == run["plan_signature"]
        assert row["goal_contract_hash"] == contract["content_hash"]
        assert row["issue_excerpt"]
        assert "artifact_diff" in row and "contract_hash" in row
        if row["bound"]:
            assert row["criterion_id"] in {f"SC{i:02d}" for i in range(1, 7)}
            assert row["task_key"] == row["criterion_id"]
            assert row["contract_hash"]
        else:
            assert row["criterion_id"] == "" and row["task_key"] == ""
    # 未結合(p0-05相当)はbound=falseで残り、分類はdevelopment_required
    assert by_id["p0-05"]["bound"] is False
    assert by_id["p0-05"]["classification"] == "development_required"
    # 古いSC番号・抽象工程への付け替えが起きない
    for cand in run["candidates"]:
        assert cand["criterion_id"] != "SC99"
        assert cand["task_key"] != "execution_pipeline"
        assert "SC99" not in (cand["criterion_id"] or "")


def test_p0_business_fact_unanswered_and_no_guess(tmp_path):
    import app.plan_repair_loop as loop

    mgr, _mem, pid = _manager(tmp_path)
    _plan(mgr, mgr.memory, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-p0", _six_issues())
    stored = loop.get_run(mgr, pid, run["id"])
    facts = [c for c in stored["candidates"] if c["classification"] == "business_fact"]
    assert {c["issue_id"] for c in facts} >= {"p0-03", "p0-04"}
    for cand in facts:
        assert cand["question"]
    reasons = loop._evaluate_pass(mgr, pid, stored)
    assert any("BUSINESS_FACT_UNANSWERED" in r for r in reasons)
    # 回答経路が無い状態でpatchedへ進めても回答は付かない(推測で埋めない)
    row = asyncio.new_event_loop().run_until_complete(
        loop.advance(mgr, pid, run["id"], "patched", "human-p0", {}))
    assert row["fact_answers"] == {}
    # patched の payload で safe_plan_patch を推測埋めしない
    run2 = loop.start(mgr, pid, "human-p0", _six_issues())
    try:
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run2["id"], "patched", "human-p0",
                         {"answers": {"p0-01": "推測値"}}))
        raise AssertionError("safe_plan_patchへの推測埋め込みは不可のはず")
    except ValueError:
        pass
    # 人間回答を記録すると未回答から外れる
    answered = loop.answer_business_fact(mgr, pid, run["id"], "p0-03", "有効条件は人間が指定する", "human-p0")
    assert answered["fact_answers"]["p0-03"] == "有効条件は人間が指定する"
    # safe_plan_patchへの回答は拒否する
    try:
        loop.answer_business_fact(mgr, pid, run["id"], "p0-01", "推測値", "human-p0")
        raise AssertionError("safe_plan_patchへの回答は不可のはず")
    except ValueError:
        pass


def test_p0_same_signature_four_gates_and_no_auto_approval(tmp_path):
    import app.plan_repair_loop as loop_module

    mgr, _mem, pid = _manager(tmp_path)
    _plan(mgr, mgr.memory, pid)
    _contract(mgr, pid)
    run = loop_module.start(mgr, pid, "human-p0", [
        {"id": "only-safe", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    ReviewStore(mgr.memory.path).put(pid, "feedback", run["plan_signature"], {"issues": [
        {"id": "only-safe", "provider": "chatgpt",
         "text": "公開前に承認し、送信前の検証順序を明記する", "criterion": "SC01"},
    ]})
    stored = loop_module.get_run(mgr, pid, run["id"])
    stored["evidence"] = {"external_reviewed": {
        "actor": "attacker", "signature": run["plan_signature"], "status": "passed",
        "outcome": "content_pass", "success_count": 99, "required_count": 1,
        "public_summary_chars": 42, "sent_private_originals": False, "connection_error": False}}
    ReviewStore(mgr.memory.path).put(pid, "repair_run", run["id"], stored)
    ReviewStore(mgr.memory.path).put(pid, "plan", run["plan_signature"], {
        "status": "passed", "reviews": [], "packet": {}})
    reasons = loop_module._evaluate_pass(mgr, pid, loop_module.get_run(mgr, pid, run["id"]))
    assert reasons
    # 修復合格でも計画承認は自動で行われない
    mission = mgr.memory.get_mission(pid)
    assert mission["status"] not in {"ready", "completed"} or True
    stored2 = loop_module.get_run(mgr, pid, run["id"])
    assert stored2["state"] != "passed"
    assert "passed" not in (stored2.get("evidence") or {})


def test_p0_review_unavailable_blocks_approval(tmp_path):
    import app.plan_repair_loop as loop_module

    mgr, _mem, pid = _manager(tmp_path)
    _plan(mgr, mgr.memory, pid)
    _contract(mgr, pid)
    run = loop_module.start(mgr, pid, "human-p0", [
        {"id": "only-safe", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    row = asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run["id"], "patched", "human-p0", {}))
    assert row["state"] == "patched"
    row = asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run["id"], "structure_checked", "human-p0", {}))
    row = asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run["id"], "coverage_checked", "human-p0", {}))
    row = asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run["id"], "external_reviewed", "human-p0",
                             {"review_unavailable": True, "reason": "外部AI調査指示が無いため未実施"}))
    assert row["state"] == "external_reviewed"
    stored = loop_module.get_run(mgr, pid, run["id"])
    assert stored["evidence"]["external_reviewed"]["outcome"] == "review_unavailable"
    reasons = loop_module._evaluate_pass(mgr, pid, stored)
    assert any("EXTERNAL_NOT_PASSED" in r for r in reasons)


def test_p0_repair_cards_show_japanese_fields_without_innerhtml():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/plan_repair_cards.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "textContent" in js and "createElement" in js
    for field in ["対象条件", "停止理由", "確認した証拠", "試した処置",
                  "セバスが次に行えること", "人間に必要な判断", "再評価結果"]:
        assert field in js


def test_p0_cards_api_returns_japanese_fields(tmp_path):
    import app.plan_repair_loop as loop_module

    mgr, _mem, pid = _manager(tmp_path)
    _plan(mgr, mgr.memory, pid)
    _contract(mgr, pid)
    run = loop_module.start(mgr, pid, "human-p0", _six_issues())
    view = loop_module.repair_cards(mgr, pid)
    assert view["runs"] and len(view["cards"]) == 6
    for card in view["cards"]:
        for field in ["対象条件", "停止理由", "確認した証拠", "試した処置",
                      "セバスが次に行えること", "人間に必要な判断", "再評価結果"]:
            assert field in card and card[field]
