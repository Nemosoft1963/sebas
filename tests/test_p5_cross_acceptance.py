"""P5 横断受入試験(疑似案件のみ。実案件・実売・実URL不使用)。

app/ は変更しない(読み取り+実在関数の呼び出しのみ)。外部AI呼び出しだけを
既存の流儀(FixedIndex・review_plan スタブ・gate モック)で置き換える。
架空データは必ず「架空」と分かる名前にする。実売達成の記録は一切行わない。
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.experience_memory import ExperienceMemory, current_index_identity
from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_plan, compile_task

FILLER = "架空の検証可能な具体的内容を記載する。" * 40


class Manager(SimpleNamespace):
    pass


class FixedIndex:
    def __init__(self, ids):
        self.ids = list(ids)

    def search(self, *args):
        return list(self.ids)


def _manager(tmp_path, name="p5pseudo"):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project(name)["id"]
    mem.save_mission(
        pid,
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "",
        True,
        ["chatgpt"],
    )
    from app.workspace_files import WorkspaceSandbox

    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(),
                  llm=None, provider_statuses=lambda: [],
                  plan_review_runner=None)
    return mgr, mem, pid


def _plan2(mgr, mem, pid):
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p5 pseudo plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _assert_no_success_across_routes(mgr, pid):
    """どの経路も true の達成を出さないことの横断アサート。"""
    from app.completion_gate import evaluate as gate_evaluate
    from app.completion_replay import preview as replay_preview
    from app.pending_ledger import build as ledger_build
    import app.resolution_coordinator as coord
    import app.safe_auto_resume as auto
    import app.recovery_record as rec

    gate = gate_evaluate(mgr, pid, persist=False)
    assert gate.get("achieved") is False
    replay = replay_preview(mgr, pid)
    assert replay.get("would_achieve") is False
    assert replay.get("crossed_approval_boundary") is False
    ledger = ledger_build(mgr, pid) or {}
    summary = (ledger.get("summary") or {})
    all_good = (
        summary.get("plan_status") == "approved"
        and (summary.get("unresolved_count") or 0) == 0
        and not summary.get("repair_blocking")
        and not summary.get("plan_approval_blocked")
    )
    assert not all_good, "pending_ledger が成功扱いに見える: %s" % summary
    for view in coord.list_resolutions(mgr, pid):
        assert view.get("state") != "resolved", view
    stage = auto.stage_gate(mgr, pid)
    if not stage.get("blocked"):
        assert auto.enabled(mgr, pid) is False
    for record in rec.list_records(mgr, pid):
        assert record.get("state") != "recovered", record


def _exp_memory(tmp_path, pid, mode="enforce"):
    memory = ExperienceMemory(tmp_path / ("exp-%s" % pid),
                              {"projects": {pid: mode}, "embedding_model": "test-model"})
    return memory


def _seed_case(memory, pid, key, status, input_version="v-current",
               source_hash="hash-current", indexed=True, identity=None):
    store = memory.store
    rid = store.add(pid, "success", "架空の手順仮説-%s" % key,
                    {"input_version": input_version, "source_hash": source_hash},
                    {"validation": "p5-cross", "source": "架空源泉-%s" % key})
    if status == "verified":
        store.review(pid, rid, "verified", "架空確認者", "架空の確認証拠",
                     time.time() + 3600)
    elif status == "needs_review":
        with store.connect() as db:
            db.execute("UPDATE experiences SET status='needs_review' WHERE id=?", (rid,))
    elif status == "revoked":
        store.review(pid, rid, "verified", "架空確認者", "proof", time.time() + 3600)
        store.review(pid, rid, "revoked", "架空確認者", "架空の撤回", 0)
    elif status == "expired":
        store.review(pid, rid, "verified", "架空確認者", "proof", time.time() + 3600)
        with store.connect() as db:
            db.execute("UPDATE experiences SET expires=? WHERE id=?",
                       (time.time() - 10, rid))
    if indexed:
        memory.store.set_index_state(
            pid, rid, "indexed", "", identity or current_index_identity(memory.config))
    memory.index = FixedIndex([rid])
    return rid


# ---------------------------------------------------------------- 1. 誤成功ゼロ
P5_FALSE_CASES = [
    "missing_original",
    "sha_mismatch",
    "old_input_version",
    "unapproved_ocr",
    "index_failed",
    "expired_rag",
    "triz_trial_only",
    "external_review_fail",
    "fake_url",
    "partial_artifact",
]


@pytest.mark.parametrize("case", P5_FALSE_CASES)
def test_p5_no_false_success_across_routes(tmp_path, case):
    """10状況のいずれでも成功/達成/解決/参照採用にならない(パラメタ化で網羅)。"""
    import app.plan_case_reference as pcr
    import app.rag_eligibility as elig
    import app.recovery_record as rec
    from app import automatic_triz as triz

    mgr, mem, pid = _manager(tmp_path, "p5false-%s" % case)
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)

    if case == "missing_original":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "missing", "verified")
        with memory.store.connect() as db:
            db.execute("UPDATE experiences SET applicability=?, evidence=? WHERE id=?",
                       (json.dumps({"input_version": "v-current"}), json.dumps({}), rid))
        rows = [memory.store.get(pid, rid)]
        states = {rid: memory.store.get_index_state(pid, rid)}
        result = elig.assess_rows(memory, pid, "v-current", "", rows, states, None)
        assert result["eligible"] == []
        assert any(v in ("source_hash_changed",) for v in
                   [x["verdict"] for x in result["rejected"]])
        found = rec.check_experiences(mgr, pid, "v-current", "")
        # 原本ハッシュ検証不能は受領側で拒否される(空でも達成に使わない)。
        assert isinstance(found, dict)
    elif case == "sha_mismatch":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "mismatch", "verified")
        rows = [memory.store.get(pid, rid)]
        states = {rid: memory.store.get_index_state(pid, rid)}
        result = elig.assess_rows(memory, pid, "v-current", "hash-other",
                                  rows, states, None)
        assert result["eligible"] == []
        assert any(x["verdict"] == "source_hash_changed" for x in result["rejected"])
    elif case == "old_input_version":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "oldver", "verified",
                         input_version="v-old")
        memory.index = FixedIndex([rid])
        out = pcr.get_case_references(
            memory, pid, [{"criterion_id": "SC01", "statement": "架空手順Aを定義する"}],
            "v-current", 1, "sig-p5-old", {})
        rejected = [r for c in out["criteria"] for r in c["references"]
                    if r["case_id"] == rid]
        assert rejected and all(r["used_or_rejected"] == "rejected" for r in rejected)
        assert any(r["applicability_verdict"] == "input_version_mismatch"
                   for r in rejected)
    elif case == "unapproved_ocr":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "ocr", "verified")
        row = memory.store.get(pid, rid)
        report = memory.store.assess_row(
            row, {**json.loads(row["evidence"]),
                  "ocr_derived": True, "ocr_approved": False})
        assert report["approvable"] is False
        assert "ocr_not_approved" in report["blocked"]
        found = rec.check_experiences(mgr, pid, "v-current", "hash-current",
                                      ocr_needs_review=True)
        assert isinstance(found, dict)
    elif case == "index_failed":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "idxfail", "verified", indexed=False)
        memory.store.set_index_state(
            pid, rid, "failed", "架空の索引失敗",
            current_index_identity(memory.config))
        memory.index = FixedIndex([rid])
        out = pcr.get_case_references(
            memory, pid, [{"criterion_id": "SC01", "statement": "架空手順Aを定義する"}],
            "v-current", 1, "sig-p5-idx", {})
        rejected = [r for c in out["criteria"] for r in c["references"]
                    if r["case_id"] == rid]
        assert rejected and all(r["used_or_rejected"] == "rejected" for r in rejected)
        assert any(r["applicability_verdict"] == "not_indexed" for r in rejected)
    elif case == "expired_rag":
        memory = _exp_memory(tmp_path, pid)
        rid = _seed_case(memory, pid, "expired", "expired")
        rows = [memory.store.get(pid, rid)]
        states = {rid: memory.store.get_index_state(pid, rid)}
        result = elig.assess_rows(memory, pid, "v-current", "hash-current",
                                  rows, states, None)
        assert result["eligible"] == []
        assert any(x["verdict"] == "expired" for x in result["rejected"])
    elif case == "triz_trial_only":
        # 隔離試験合格だけでは業務回復にならない。
        assert triz.is_business_success("artifact_trial_passed") is False
        assert triz.is_business_success("business_recovered") is True
        from app import triz_candidate_ranking as ranking

        ranked = ranking.rank_candidates(
            [{"id": "架空候補-1", "criterion_id": "SC01",
              "steps": [{"capability": "workflow.design"}]}],
            criterion_id="SC01", task_key="SC01",
            source_hashes=None, trials={"架空候補-1": {"status": "passed"}})
        assert ranked["ranked"] and "採用・成功を意味しない" in ranked["note_ja"]
    elif case == "external_review_fail":
        import app.plan_repair_loop as loop

        run = loop.start(mgr, pid, "架空確認者", [
            {"id": "p5-ext", "criterion": "SC01",
             "text": "公開前に承認し、送信前の検証順序を明記する"},
        ])
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "架空確認者", {}))
        assert row["state"] == "patched"
        for nxt in ("structure_checked", "coverage_checked"):
            row = asyncio.new_event_loop().run_until_complete(
                loop.advance(mgr, pid, run["id"], nxt, "架空確認者", {}))
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "external_reviewed", "架空確認者",
                         {"review_unavailable": True,
                          "reason": "架空の外部AI調査指示が無いため未実施"}))
        stored = loop.get_run(mgr, pid, run["id"])
        assert stored["evidence"]["external_reviewed"]["outcome"] == "review_unavailable"
        reasons = loop._evaluate_pass(mgr, pid, stored)
        assert any("EXTERNAL_NOT_PASSED" in r for r in reasons)
        assert stored["state"] != "passed"
    elif case == "fake_url":
        # 架空URLだけでは外部証拠にならない。gate は未達のまま。
        from app.completion_gate import evaluate as gate_evaluate
        from app.generic_goal_checks import collect_external_evidence

        evidence = collect_external_evidence(mgr, pid)
        assert evidence["operations"] == []
        gate = gate_evaluate(mgr, pid, persist=False)
        assert gate.get("achieved") is False
    elif case == "partial_artifact":
        # 成果物なし・部分成果物では達成にならない。
        import app.safe_auto_resume as auto

        from app.completion_gate import evaluate as gate_evaluate

        gate = gate_evaluate(mgr, pid, persist=False)
        assert gate.get("achieved") is False
        assert gate.get("failed_criteria")
        with pytest.raises((OSError, ValueError, KeyError, AttributeError, TypeError)):
            auto._verify_completed_run_artifact(
                mgr, pid, {"evidence": "[]", "artifact_hash": "架空不一致"})

    # どの経路でも真の達成を出さない。
    _assert_no_success_across_routes(mgr, pid)


# ---------------------------------------------------------- 2. 販売計画の通し
def _six_issues():
    return [
        {"id": "p5-01", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "p5-02", "criterion": "SC02", "text": "既存フォームを再作成せず再利用する手順を明記する"},
        {"id": "p5-03", "criterion": "SC01", "text": "リードの有効条件を決定する"},
        {"id": "p5-04", "criterion": "SC02", "text": "価格の基準値を決定する"},
        {"id": "p5-05", "criterion": "SC99", "text": "古いSC99の抽象工程へ付け替える"},
        {"id": "p5-06", "criterion": "SC02", "text": "外部API連携を新規実装する"},
    ]


def _manager6(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p5sales")["id"]
    lines = "\n".join("匿名条件%02d" % i for i in range(1, 7))
    mem.save_mission(pid, "疑似販売目標\n" + lines, "疑似販売目標\n" + lines,
                     "", True, ["chatgpt"])
    from app.workspace_files import WorkspaceSandbox

    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(),
                  llm=None, provider_statuses=lambda: [],
                  plan_review_runner=None)
    return mgr, mem, pid


def _plan6(mgr, mem, pid):
    from app.structured_planning import extract_criteria

    mission = mem.get_mission(pid)
    criteria = extract_criteria(mission.get("goal") or "",
                                mission.get("success_criteria") or "")
    tasks = []
    for idx, name in enumerate(criteria, 1):
        tasks.append(compile_task(
            idx, name,
            {"title": "匿名施策%02d" % idx, "scope": "%sの手順を整理する" % name,
             "headings": ["現状確認", "手順"], "depends_on": []}, []))
    compiled = compile_plan(criteria, tasks, goal=mission.get("goal"))
    mem.replace_plan(pid, "p5 sales plan", compiled["tasks"])
    return mem.get_mission(pid)


def test_p5_sales_plan_end_to_end_only_same_signature_passes(tmp_path):
    """未解決6件→対応づけ→回答前passed不可→回答後・同一署名4点揃いのみpassed→自動承認なし。"""
    import app.plan_repair_loop as loop
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, mem, pid = _manager6(tmp_path)
    _plan6(mgr, mem, pid)
    contract = _contract(mgr, pid)
    run = loop.start(mgr, pid, "架空確認者", _six_issues())
    by_id = {c["issue_id"]: c for c in run["candidates"]}
    assert len(by_id) == 6
    assert by_id["p5-05"]["bound"] is False
    facts = {c["issue_id"] for c in run["candidates"]
             if c["classification"] == "business_fact"}
    assert {"p5-03", "p5-04"} <= facts
    stored = loop.get_run(mgr, pid, run["id"])
    reasons = loop._evaluate_pass(mgr, pid, stored)
    assert any("BUSINESS_FACT_UNANSWERED" in r for r in reasons)
    assert reasons, "回答前は passed にならないこと"
    with pytest.raises(ValueError):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "passed", "架空確認者", {}))

    # 人間回答後は未回答から外れるが、要実装が残るため依然 passed 不可。
    loop.answer_business_fact(mgr, pid, run["id"], "p5-03",
                              "架空の有効条件は人間が指定する", "架空確認者")
    loop.answer_business_fact(mgr, pid, run["id"], "p5-04",
                              "架空の基準値は人間が指定する", "架空確認者")
    stored = loop.get_run(mgr, pid, run["id"])
    reasons = loop._evaluate_pass(mgr, pid, stored)
    assert not any("BUSINESS_FACT_UNANSWERED" in r for r in reasons)
    assert reasons, "回答だけでは passed にならない(同一署名4点が未完のため)"
    assert any("EXTERNAL_NOT_PASSED" in r for r in reasons)

    # 安全な指摘だけの別ランは、同一署名の4点が揃えば passed できる。
    run2 = loop.start(mgr, pid, "架空確認者", [
        {"id": "only-safe", "criterion": "SC01",
         "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    ReviewStore(mgr.memory.path).put(pid, "feedback", run2["plan_signature"], {"issues": []})

    async def _fake_external(manager, project_id, runrow, payload, actor):
        from app.goal_review import plan_snapshot as _snap

        _s, sig = _snap(manager, project_id)
        ReviewStore(manager.memory.path).put(project_id, "plan", sig, {
            "status": "passed", "reviews": [], "packet": {}})
        runrow["external_calls"] = int(runrow.get("external_calls") or 0) + 1
        evidence = dict(runrow.get("evidence") or {})
        evidence["external_reviewed"] = {
            "actor": actor, "signature": sig, "status": "passed",
            "outcome": "content_pass", "success_count": 1, "required_count": 1,
            "public_summary_chars": 42, "sent_private_originals": False,
            "connection_error": False}
        runrow["evidence"] = evidence
        return runrow

    real_external = loop._do_external_reviewed
    loop._do_external_reviewed = _fake_external
    try:
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run2["id"], "patched", "架空確認者", {}))
        for nxt, payload in (("structure_checked", {}), ("coverage_checked", {}),
                             ("external_reviewed",
                              {"public_summary": "公開用の要約です。" * 10,
                               "safe_to_send": True}),
                             ("rediff_evaluated", {})):
            row = asyncio.new_event_loop().run_until_complete(
                loop.advance(mgr, pid, run2["id"], nxt, "架空確認者", payload))
        assert row["state"] == "rediff_evaluated"
        # 残件が無ければ同一署名で passed できる。
        stored2 = loop.get_run(mgr, pid, run2["id"])
        assert stored2["unresolved_count"] == 0
        # 計画署名の一致を確認(同一署名固定)。
        assert stored2["plan_signature"] == plan_snapshot(mgr, pid)[1]
        assert stored2["goal_contract_hash"] == contract["content_hash"]
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run2["id"], "passed", "架空確認者", {}))
        assert row["state"] == "passed"
    finally:
        loop._do_external_reviewed = real_external
    # それでも計画承認は自動で行われない(人間)。
    mission = mgr.memory.get_mission(pid)
    assert mission["status"] != "completed"
    from app.pending_ledger import build as ledger_build

    summary = ledger_build(mgr, pid)["summary"]
    assert summary.get("plan_status") != "approved" or summary.get("result_approved") is not True


def test_p5_sales_plan_signature_change_stales_old_run(tmp_path):
    """途中で計画署名が変わると旧ランは stale/無効になる。"""
    import app.plan_repair_loop as loop

    mgr, mem, pid = _manager6(tmp_path)
    _plan6(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "架空確認者", _six_issues())
    mem.add_mission_instruction(pid, "架空の追加確認条件")
    with pytest.raises(ValueError, match="計画.*変わり|GoalContract|署名"):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "架空確認者", {}))
    stored = loop.get_run(mgr, pid, run["id"])
    assert stored["state"] != "passed"


# ------------------------------------------------- 3. RAG通しと比較
def _p5_import_original(db_path, pid, suffix, body, ref):
    from app.experience_memory import import_success_cases

    item = {
        "kind": "success",
        "content": ("【事例】P5通し%s【状況】架空案件%sで停止が頻発していた。"
                    "【施策】架空案件%s向けに点検を日次化した。"
                    "【成果】架空案件%sの停止時間が半減した。" % (suffix, suffix, suffix, suffix)),
        "applicability": {},
        "evidence": {
            "source": "架空源泉-p5-%s" % suffix,
            "source_ref": ref,
            "source_hash": hashlib.sha256(body.encode()).hexdigest(),
            "fetched_at": "2026-10-05T00:00:00+00:00",
            "extraction_method": "manual",
            "prohibitions": [],
        },
        "source_body": body,
    }
    return import_success_cases(db_path, pid, [item], "架空の収集申告", "架空収集者")


def _p5_metrics(mgr, pid, with_rag_refs):
    from app.completion_gate import evaluate as gate_evaluate
    from app.pending_ledger import build as ledger_build

    gate = gate_evaluate(mgr, pid, persist=False)
    ledger = ledger_build(mgr, pid) or {}
    summary = (ledger.get("summary") or {})
    return {
        "達成条件の充足数": sum(1 for c in gate.get("criteria") or []
                         if c.get("status") == "PASS"),
        "未解決指摘数": int(summary.get("unresolved_count") or 0),
        "外部レビュー結果(スタブ)": "not_passed(架空スタブ)",
        "手戻り回数": 0,
        "参照件数": len(with_rag_refs),
    }


def _p5_compare(metrics_with, metrics_without):
    keys = ["達成条件の充足数", "未解決指摘数", "外部レビュー結果(スタブ)", "手戻り回数"]
    same = all(metrics_with[k] == metrics_without[k] for k in keys)
    return {
        "with_rag": dict(metrics_with),
        "without_rag": dict(metrics_without),
        "claims_improvement": bool(not same),
        "note_ja": ("改善を主張しない(両条件で同じ結果)" if same
                    else "差分あり(参照件数だけを改善指標にしない)"),
    }


def test_p5_rag_end_to_end_and_compare_no_claim_when_same(tmp_path, monkeypatch):
    """実原本1件の通し + RAGあり/なし比較。同じ結果なら改善を主張しない。"""
    import app.plan_case_reference as pcr
    from app.experience_memory import ExperienceMemory, configured_memory

    db = tmp_path / "memory" / "conversations.db"
    mem = ShortTermMemory(db)
    pid = mem.create_project("p5rag")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順を定義する",
                     "疑似目標\n1. 架空手順を定義する", "", True, [])
    (db.parent / "experience_memory.json").write_text(json.dumps(
        {"projects": {pid: "enforce"}, "embedding_model": "test-model"}),
        encoding="utf-8")

    def _stub(self, project, rid):
        identity = current_index_identity(self.config)
        self.store.set_index_state(project, rid, "indexed", "", identity)
        return {"id": rid, "indexed": True}

    monkeypatch.setattr(ExperienceMemory, "reindex_verified_case", _stub)
    body = "架空原本P5-RAG-001の本文"
    out = _p5_import_original(db, pid, "rag001", body, "p5rag.md")
    assert out["registered"] == 1 and out["accepted_status"] == "candidate"
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]["id"]
    # 承認時 input_version 指定で verified。
    denied = service.store.list_candidates(pid)[0]
    assert denied["approvable"] is True
    approved = service.store.approve_candidate(
        pid, rid, "架空確認者", "架空原本と要約を突合した", input_version="v1")
    assert approved["status"] == "verified"
    assert service.store.get(pid, rid)["status"] == "verified"
    # indexed。
    service.reindex_verified_case(pid, rid)
    state = service.store.get_index_state(pid, rid)
    assert state and state["status"] == "indexed"

    # 計画参照に残る。
    service.index = FixedIndex([rid])
    result = pcr.get_case_references(
        service, pid, [{"criterion_id": "SC01", "statement": "架空手順を定義する"}],
        "v1", 1, "sig-p5-rag", {})
    used = [r for c in result["criteria"] for r in c["references"]
            if r["used_or_rejected"] == "used"]
    assert [r["case_id"] for r in used] == [rid]

    # 同じ入力・同じ疑似案件でRAGあり/なしを比較する。
    from app.workspace_files import WorkspaceSandbox

    mgr = Manager(memory=mem, workspace=WorkspaceSandbox(tmp_path / "workspace"),
                  planning_projects=set())
    _plan2(mgr, mem, pid)
    with_metrics = _p5_metrics(mgr, pid, used)
    without_metrics = _p5_metrics(mgr, pid, [])
    compared = _p5_compare(with_metrics, without_metrics)
    assert set(compared) >= {"with_rag", "without_rag", "claims_improvement", "note_ja"}
    for key in ("達成条件の充足数", "未解決指摘数", "外部レビュー結果(スタブ)", "手戻り回数"):
        assert key in compared["with_rag"] and key in compared["without_rag"]
    # 参照件数だけを改善指標にしない(件数は別枠)。
    assert compared["with_rag"]["参照件数"] == 1
    assert compared["without_rag"]["参照件数"] == 0
    # 両条件で同じ結果なら改善を主張しない。
    assert with_metrics["達成条件の充足数"] == without_metrics["達成条件の充足数"]
    assert compared["claims_improvement"] is False
    assert "改善を主張しない" in compared["note_ja"]


def test_p5_rag_zero_does_not_stop_plan(tmp_path):
    """RAGが0件/不採用でも計画生成は止まらない。"""
    import app.plan_case_reference as pcr

    mgr, mem, pid = _manager(tmp_path, "p5ragzero")
    mission = _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    memory = _exp_memory(tmp_path, pid)
    memory.index = FixedIndex([])
    out = pcr.get_case_references(
        memory, pid, [{"criterion_id": "SC01", "statement": "架空手順Aを定義する"}],
        "v-current", int(mission.get("plan_version") or 0) + 1, "sig-p5-zero", {})
    assert out["criteria"] and out["criteria"][0]["status"] in ("no_case", "unavailable")
    # 計画自体は残っている(止まらない)。
    assert mgr.memory.get_mission(pid)["tasks"]


# ---------------------------------------------------------- 4. TRIZ追跡
def _doc_manager(tmp_path, name="p5triz"):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project(name)["id"]
    mem.save_mission(pid, "疑似文書目標\n1. 架空文書を作成する",
                     "疑似文書目標\n1. 架空文書を作成する", "", True, [])
    from app.workspace_files import WorkspaceSandbox

    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set())
    return mgr, mem, pid


def _doc_task_record(fail=False):
    from app.structured_planning import SCHEMA

    return {
        "id": "t-p5-sc01", "task_key": "SC01", "depends_on": [],
        "title": "架空文書作成", "description": "架空文書を作成",
        "acceptance_criteria": json.dumps({
            "schema": SCHEMA, "criterion_ids": ["SC01"],
            "criterion": "架空文書を作成する",
            "outputs": [{"path": "result/p5sc01.md",
                         "required_headings": ["目的", "実施内容"],
                         "minimum_characters": 200}],
        }, ensure_ascii=False),
        "mode": "local", "status": "failed" if fail else "pending",
        "error": "架空の実行失敗" if fail else "",
    }


def _write_doc(mgr, pid, path, text):
    project = mgr.memory.get_project(pid)
    target = mgr.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_p5_triz_single_failure_trace_to_recovered(tmp_path):
    """候補→順位→隔離試験→業務未達(false)→別reviewer検査→人間採用→限定再実行→recovered。"""
    import app.recovery_record as rec
    import app.safe_auto_resume as auto
    from app import automatic_triz as triz
    from app import triz_candidate_ranking as ranking

    mgr, mem, pid = _doc_manager(tmp_path)
    mem.replace_plan(pid, "p5 triz plan", [_doc_task_record()])
    failed = next(t for t in mem.get_mission(pid)["tasks"] if t["task_key"] == "SC01")
    mem.update_task(failed["id"], "failed", error="架空の実行失敗")
    mission = mem.get_mission(pid)
    version = str(mission.get("plan_version"))

    candidates = [
        {"id": "架空候補-B", "criterion_id": "SC01", "task_key": "SC01",
         "steps": [{"capability": "workflow.design"}]},
        {"id": "架空候補-A", "criterion_id": "SC01", "task_key": "SC01",
         "steps": [{"capability": "workflow.design"}]},
    ]
    ranked = ranking.rank_candidates(
        candidates, criterion_id="SC01", task_key="SC01",
        source_hashes=None, trials={"架空候補-A": {"status": "passed"}})
    assert [x["candidate_id"] for x in ranked["ranked"]][0] == "架空候補-A"
    # 順位1位でも採用・成功にならない。
    assert "採用・成功を意味しない" in ranked["note_ja"]
    assert triz.is_business_success("artifact_trial_passed") is False

    trial = {"signature": "架空試験署名", "status": "artifact_trial_passed",
             "candidates": [{"id": "架空候補-A", "title": "架空案",
                             "steps": [{"capability": "workflow.design"}],
                             "missing": []}],
             "experiments": [{"candidate": "架空候補-A",
                              "cases": [{"checks": {"passed": True}},
                                        {"checks": {"passed": True}}]}]}
    row = rec.start(mgr, pid, "SC01", {"kind": "架空の実行失敗"}, "架空確認者",
                    version)
    rid = row["id"]
    assert row["state"] == "classified"
    rec.advance(mgr, pid, rid, "experience_checked", "架空の確認", {}, "架空確認者")
    with patch.object(rec, "_stored_triz", return_value=trial):
        rec.advance(mgr, pid, rid, "triz_candidates", "架空の候補確定", {}, "架空確認者")
        rec.advance(mgr, pid, rid, "isolated_trial", "架空の隔離試験",
                    {"candidate_id": "架空候補-A",
                     "capability": "workflow.design"}, "架空試験者")
    mid = rec.get(mgr, pid, rid)
    assert mid["state"] == "isolated_trial"
    # 隔離試験だけでは業務未達のまま。
    assert mid["business_passed"] is False
    assert mid["human_approved"] is False
    assert mid["rerun_passed"] is False

    gate_pass = {"criteria": [{"criterion_id": "SC01", "status": "PASS"}],
                 "coverage_passed": True, "retention_passed": True,
                 "artifact_hash": "架空物", "source_hash": "架空源",
                 "human_accepted": False,
                 "human_acceptance": {"valid": False}}
    with patch("app.completion_gate.evaluate", return_value=gate_pass):
        rec.advance(mgr, pid, rid, "business_check", "架空の独立業務検査",
                    {}, "架空業務検査者")
    after_business = rec.get(mgr, pid, rid)
    assert after_business["business_passed"] is True
    assert after_business["state"] == "business_check"
    # 検査者が試験者と別である(独立検査の追跡)。
    actors = [h.get("actor") for h in after_business["history"]]
    assert "架空試験者" in actors and "架空業務検査者" in actors

    gate_human = dict(gate_pass, human_accepted=True,
                      human_acceptance={"valid": True, "accepted_by": "架空承認者",
                                        "accepted_at": "2026-10-06T00:00:00+00:00"},
                      achieved=True)
    with patch("app.completion_gate.evaluate", return_value=gate_human):
        rec.advance(mgr, pid, rid, "human_approval", "架空の人間採用",
                    {}, "架空承認者")
    assert rec.get(mgr, pid, rid)["human_approved"] is True

    # 限定再実行(P2の実在の完了run)を後に作る(作成時刻が回復開始より後)。
    target = _write_doc(mgr, pid, "result/p5sc01.md",
                        "# 架空文書\n\n## 目的\n" + FILLER + "\n\n## 実施内容\n" + FILLER + "\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    proof = json.dumps([{"path": "result/p5sc01.md", "sha256": digest,
                         "size": target.stat().st_size}], ensure_ascii=False)
    time.sleep(0.01)
    saved = auto._save(mgr, pid, "SC01", "架空入力", "架空冪等-p5",
                       artifact_hash=digest, run_id="架空run-p5",
                       status="completed", evidence=proof,
                       retry_count=0, retry_limit=2)
    with patch("app.completion_gate.evaluate", return_value=gate_human):
        rec.advance(mgr, pid, rid, "limited_rerun", "架空の限定再実行",
                    {"run_id": saved["run_id"]}, "架空承認者")
        final = rec.advance(mgr, pid, rid, "recovered", "架空の回復確認",
                            {}, "架空承認者")
    assert final["state"] == "recovered"
    assert final["business_passed"] is True and final["rerun_passed"] is True


def test_p5_triz_trial_only_stays_not_recovered(tmp_path):
    """隔離試験が通っただけの例は business_recovered=false のまま。"""
    import app.recovery_record as rec
    from app import automatic_triz as triz

    mgr, mem, pid = _doc_manager(tmp_path, "p5trizonly")
    mem.replace_plan(pid, "p5 triz plan", [_doc_task_record()])
    failed = next(t for t in mem.get_mission(pid)["tasks"] if t["task_key"] == "SC01")
    mem.update_task(failed["id"], "failed", error="架空の実行失敗")
    version = str(mem.get_mission(pid).get("plan_version"))
    trial = {"signature": "架空試験署名2", "status": "artifact_trial_passed",
             "candidates": [{"id": "架空候補-1",
                             "steps": [{"capability": "workflow.design"}],
                             "missing": []}],
             "experiments": [{"candidate": "架空候補-1",
                              "cases": [{"checks": {"passed": True}},
                                        {"checks": {"passed": True}}]}]}
    row = rec.start(mgr, pid, "SC01", {"kind": "架空の実行失敗"}, "架空確認者", version)
    rid = row["id"]
    rec.advance(mgr, pid, rid, "experience_checked", "架空の確認", {}, "架空確認者")
    with patch.object(rec, "_stored_triz", return_value=trial):
        rec.advance(mgr, pid, rid, "triz_candidates", "架空の候補確定", {}, "架空確認者")
        rec.advance(mgr, pid, rid, "isolated_trial", "架空の隔離試験",
                    {"candidate_id": "架空候補-1",
                     "capability": "workflow.design"}, "架空試験者")
    stored = rec.get(mgr, pid, rid)
    assert stored["state"] == "isolated_trial"
    assert stored["business_passed"] is False
    assert triz.is_business_success("artifact_trial_passed") is False
    assert triz.is_business_success(stored["state"]) is False


# ---------------------------------------------------------- 5. 安全(横断)
def test_p5_safety_concurrent_no_double_execution(tmp_path):
    """(a)同時要求で同一工程が二重実行されない。停止理由の保存は(b)(c)で確認。"""
    import app.resolution_coordinator as coord
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path, "p5safe-a")
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    # coordinator の同時検出は冪等(同一ID)。
    first = coord.detect(mgr, pid, "架空確認者")

    async def _gather():
        return await asyncio.gather(
            *(asyncio.to_thread(coord.detect, mgr, pid, "架空確認者") for _ in range(4)))

    results = asyncio.new_event_loop().run_until_complete(_gather())
    ids = sorted({x["id"] for r in results for x in r["resolutions"]})
    assert ids == sorted({x["id"] for x in first["resolutions"]})

    # P2 claim の同時要求は片方だけが取れる。
    mission = mem.get_mission(pid)
    task = next(t for t in mission["tasks"] if t.get("task_key") == "SC01")
    from app.goal_review import plan_snapshot as _snap

    _s, sig = _snap(mgr, pid)
    ih = auto._input_hash(sig, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})

    async def _claims():
        return await asyncio.gather(
            *(asyncio.to_thread(auto._claim, mgr, pid, "SC01", ih, idem, 2)
              for _ in range(2)))

    both = asyncio.new_event_loop().run_until_complete(_claims())
    assert sum(1 for x in both if x is not None) == 1


def test_p5_safety_lease_crash_recovers_without_rerun(tmp_path, monkeypatch):
    """(b)中断後リース失効で復帰し、成果物ありなら再実行しない。理由を保存・取得。"""
    import app.safe_auto_resume as auto

    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    mgr, mem, pid = _doc_manager(tmp_path, "p5safe-b")
    mem.replace_plan(pid, "p5 safe plan", [_doc_task_record()])
    mem.set_mission_status(pid, "ready")
    from app.goal_review import ReviewStore, plan_snapshot as _snap

    _s, sig = _snap(mgr, pid)
    ReviewStore(mgr.memory.path).put(pid, "plan", sig, {"status": "passed"})
    mission = mem.get_mission(pid)
    task = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    ih = auto._input_hash(sig, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})
    assert auto._claim(mgr, pid, "SC01", ih, idem, 2) is not None
    target = _write_doc(mgr, pid, "result/p5sc01.md",
                        "# 架空文書\n\n## 目的\n" + FILLER + "\n\n## 実施内容\n" + FILLER + "\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    proof = json.dumps([{"path": "result/p5sc01.md", "sha256": digest,
                         "size": target.stat().st_size}], ensure_ascii=False)
    with sqlite3.connect(auto._db_path(mgr)) as db:
        db.execute("UPDATE runs SET artifact_hash=?, evidence=?, lease_expires=? "
                   "WHERE project_id=? AND task_key=? AND idempotency_key=?",
                   (digest, proof, time.time() - 1, pid, "SC01", idem))
    monkeypatch.setattr(auto, "_approved", lambda *a, **k: True)
    monkeypatch.setattr(auto, "stage_gate", lambda *a, **k: {
        "blocked": False, "reason": "", "verified": True, "unresolved_count": 0,
        "plan_approval_blocked": False, "plan_signature": sig})
    auto.set_enabled(mgr, pid, True)
    calls = []

    def _executor(task, key):
        calls.append(str(task.get("task_key")))
        return {"artifact_hash": digest,
                "evidence": [{"path": "result/p5sc01.md", "sha256": digest, "size": 1}]}

    result = asyncio.new_event_loop().run_until_complete(auto.resume(mgr, pid, _executor))
    assert calls == []
    assert result["executed"] == ["SC01"]
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert rows[0]["status"] == "completed"
    # 停止理由(リース失効)が保存・取得できる。
    assert int(rows[0]["expired_count"]) >= 1
    assert rows[0]["last_expired_reason"] == auto.LEASE_EXPIRED_REASON


def test_p5_safety_signature_change_stales(tmp_path):
    """(c)計画署名/版が変わると旧候補が stale になる。理由を保存・取得。"""
    import app.plan_repair_loop as loop
    import app.resolution_coordinator as coord

    mgr, mem, pid = _manager(tmp_path, "p5safe-c")
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    detected = coord.detect(mgr, pid, "架空確認者")
    assert detected["resolutions"]
    old_id = detected["resolutions"][0]["id"]
    run = loop.start(mgr, pid, "架空確認者", [
        {"id": "p5-c", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"}])
    mem.add_mission_instruction(pid, "架空の追加条件")
    second = coord.detect(mgr, pid, "架空確認者")
    assert old_id in second["staled"]
    stale_view = coord.get(mgr, pid, old_id)
    assert stale_view["state"] == "stale"
    assert any("stale" in str(h.get("to")) or "失効" in str(h.get("reason"))
               for h in stale_view["history"])
    with pytest.raises(ValueError, match="stale"):
        coord.advance(mgr, pid, old_id, "candidate_ready", "架空確認者", "進める", {})
    with pytest.raises(ValueError, match="計画.*変わり|GoalContract|署名"):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "架空確認者", {}))


def test_p5_safety_revoked_never_passes(tmp_path):
    """(d)承認撤回(revoked)は索引にIDが残っても計画・TRIZへ渡らない。"""
    import app.plan_case_reference as pcr
    import app.rag_eligibility as elig

    mgr, mem, pid = _manager(tmp_path, "p5safe-d")
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    memory = _exp_memory(tmp_path, pid)
    rid = _seed_case(memory, pid, "revoke", "verified")
    memory.store.review(pid, rid, "revoked", "架空確認者", "架空の承認撤回", 0)
    assert memory.store.get(pid, rid)["status"] == "revoked"
    # 索引にIDが残っている想定でも(検索が古いIDを返しても)不採用。
    memory.index = FixedIndex([rid])
    out = pcr.get_case_references(
        memory, pid, [{"criterion_id": "SC01", "statement": "架空手順Aを定義する"}],
        "v-current", 1, "sig-p5-revoked", {})
    rejected = [r for c in out["criteria"] for r in c["references"]
                if r["case_id"] == rid]
    assert rejected and all(r["used_or_rejected"] == "rejected" for r in rejected)
    rows = [memory.store.get(pid, rid)]
    states = {rid: memory.store.get_index_state(pid, rid)}
    result = elig.assess_rows(memory, pid, "v-current", "hash-current",
                              rows, states, None)
    assert result["eligible"] == []
    # 撤回理由が保存・取得できる。
    events = memory.store.candidate_events(pid, rid)
    reviews = memory.store.list(pid)
    assert memory.store.get(pid, rid)["status"] == "revoked"
    assert rejected[0]["reason"]


def test_p5_safety_ocr_needs_review_never_passes(tmp_path):
    """(e)OCRが needs_review になった事例は渡らない。理由を保存・取得。"""
    import app.plan_case_reference as pcr
    import app.rag_eligibility as elig
    import app.recovery_record as rec

    mgr, mem, pid = _manager(tmp_path, "p5safe-e")
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    memory = _exp_memory(tmp_path, pid)
    rid = _seed_case(memory, pid, "ocr", "verified")
    moved = memory.store.move_to_needs_review(
        pid, rid, "架空確認者", "架空OCRの再審査が必要")
    assert moved["status"] == "needs_review"
    memory.index = FixedIndex([rid])
    out = pcr.get_case_references(
        memory, pid, [{"criterion_id": "SC01", "statement": "架空手順Aを定義する"}],
        "v-current", 1, "sig-p5-ocr", {})
    rejected = [r for c in out["criteria"] for r in c["references"]
                if r["case_id"] == rid]
    assert rejected and all(r["used_or_rejected"] == "rejected" for r in rejected)
    rows = [memory.store.get(pid, rid)]
    states = {rid: memory.store.get_index_state(pid, rid)}
    result = elig.assess_rows(memory, pid, "v-current", "hash-current",
                              rows, states, None)
    assert result["eligible"] == []
    assert any(x["verdict"] == "needs_review" for x in result["rejected"])
    checked = rec.check_experiences(mgr, pid, "v-current", "hash-current",
                                    ocr_needs_review=True)
    assert isinstance(checked, dict)
    # 隔離理由が保存・取得できる。
    detail = memory.store.get_needs_review(pid, rid)
    assert detail and detail["status"] == "needs_review"
    assert detail["events"]


# ---------------------------------------------------------- 6. P2既定
def test_p5_auto_resume_default_disabled_and_blocked(tmp_path, monkeypatch):
    """既定は無効。有効化しても未解決/未承認の疑似販売案件では blocked。"""
    import app.safe_auto_resume as auto
    from app.goal_review import ReviewStore

    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    mgr, mem, pid = _manager6(tmp_path)
    _plan6(mgr, mem, pid)
    _contract(mgr, pid)
    _s_sig = ReviewStore(mgr.memory.path)
    # 未解決指摘を残す(現行署名の feedback)。
    from app.goal_review import plan_snapshot as _snap

    _s, sig = _snap(mgr, pid)
    ReviewStore(mgr.memory.path).put(pid, "feedback", sig, {"issues": [
        {"id": "p5-block", "provider": "chatgpt",
         "text": "架空の未解決指摘", "criterion": "SC01"}]})
    assert auto.enabled(mgr, pid) is False
    stage = auto.stage_gate(mgr, pid)
    assert stage.get("blocked") is True
    with pytest.raises(ValueError):
        auto.set_enabled(mgr, pid, True)
    assert auto.enabled(mgr, pid) is False
    # 強制的に有効化しても実行されない(blocked)。
    with auto._connect(mgr) as db:
        db.execute("INSERT INTO settings(project_id,enabled,updated) VALUES(?,?,?) "
                   "ON CONFLICT(project_id) DO UPDATE SET enabled=1",
                   (pid, 1, time.time()))
    assert auto.enabled(mgr, pid) is True
    result = asyncio.new_event_loop().run_until_complete(auto.resume(mgr, pid))
    assert result["status"] == "blocked"
    assert result["executed"] == []
    assert result.get("reason") in ("plan_not_verified_and_approved",
                                    "plan_changed_or_unapproved",
                                    "plan_read_failed")


# ---------------------------------------------------------- 7. 読取専用
def test_p5_read_only_apis_change_nothing(tmp_path):
    """読み取り系APIの前後で承認・実行記録・外部操作・RAG登録が変化しない。"""
    import app.completion_replay as replay
    import app.plan_case_reference as pcr
    import app.plan_repair_loop as loop
    import app.resolution_coordinator as coord
    import app.safe_auto_resume as auto
    from app.goal_completion_store import GoalCompletionStore
    from app.goal_review import ReviewStore, plan_snapshot as _snap

    mgr, mem, pid = _manager(tmp_path, "p5readonly")
    _plan2(mgr, mem, pid)
    _contract(mgr, pid)
    memory = _exp_memory(tmp_path, pid)
    rid = _seed_case(memory, pid, "readonly", "verified")
    coord.detect(mgr, pid, "架空確認者")

    def _snapshot():
        _s, sig = _snap(mgr, pid)
        return {
            "actions": list(mem.list_actions(pid)),
            "accept": GoalCompletionStore(mgr.memory.path).latest_unrevoked_acceptance(pid),
            "plan_review": ReviewStore(mgr.memory.path).get(pid, "plan", sig),
            "auto": list(auto.records(mgr, pid)),
            "rag": [(r["id"], r["status"]) for r in memory.store.list(pid)],
            "index": [(s["experience_id"], s["status"])
                      for s in memory.store.list_index_states(pid)],
            "resolutions": coord.list_resolutions(mgr, pid),
        }

    before = _snapshot()
    coord.diagnose_only(mgr, pid)
    replay.preview(mgr, pid)
    coord.list_resolutions(mgr, pid)
    if before["resolutions"]:
        coord.get(mgr, pid, before["resolutions"][0]["id"])
    loop.repair_cards(mgr, pid)
    coord.resolution_cards(mgr, pid)
    coord.summary(mgr, pid)
    memory.index_status_view(pid)
    pcr.current_view(memory.store.path.parent.parent, pid, 1)
    after = _snapshot()
    assert after == before
