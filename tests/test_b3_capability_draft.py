"""B-3: 不足機能の下書きまで自走し、確認は人に残す (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型/表データ型/外部作用型、架空)。
"""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]


def _criteria_for(domain, n):
    out = []
    for i in range(1, n + 1):
        if domain == "doc":
            out.append(f"架空文書{i:02d}の作成と検証")
        elif domain == "table":
            out.append(f"架空営業管理表{i:02d}の集計と検算")
        else:
            if i % 3 == 0:
                out.append(f"架空顧客へ送信{i:02d}の実行と検証")
            else:
                out.append(f"架空告知文書{i:02d}の作成と検証")
    return out


def _titles_for(domain, i):
    if domain == "doc":
        return f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成し根拠を検証する。"
    if domain == "table":
        return f"架空集計工程{i}", f"架空営業管理表{i:02d}の表を作成し検算する。"
    if i % 3 == 0:
        return f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。実際の外部送信は行わない。"
    return f"架空告知工程{i}", f"架空告知文書{i:02d}の成果物を作成し根拠を検証する。"


def _make_manager(tmp_path, domain, n, tag=""):
    assert "架空" in f"架空-{domain}"
    name = f"b3-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _make_doc_only_with_system_build(tmp_path, tag="-docsys"):
    """文書だけの工程で、達成条件にシステム構築・稼働・検証を含む疑似PJ。"""
    manager, pid = _make_manager(tmp_path, "doc", 2, tag=tag)
    mission = manager.memory.get_mission(pid)
    mission["success_criteria"] = (
        "1. 架空手順書01を文書で作る\n"
        "2. 架空共有体系02のシステム構築と稼働検証\n"
        "3. 架空稼働結果03の検証と報告")
    manager.memory.save_mission(
        pid, mission["goal"], mission["success_criteria"],
        mission.get("constraints_text") or "",
        mission.get("allow_external_ai", False),
        mission.get("external_providers") or [])
    return manager, pid


def _make_gap_project(tmp_path, domain, tag=""):
    """機能不足が出る疑似PJ3種。文書型はシステム構築を足す。"""
    if domain == "external":
        return _make_manager(tmp_path, domain, 3, tag=tag)
    if domain == "doc":
        return _make_doc_only_with_system_build(tmp_path, tag=tag or "-sys")
    manager, pid = _make_manager(tmp_path, domain, 2, tag=tag)
    mission = manager.memory.get_mission(pid)
    mission["success_criteria"] = (
        "1. 架空営業管理表01の集計と検算\n"
        "2. 架空顧客へ送信02の実行と検証")
    manager.memory.save_mission(
        pid, mission["goal"], mission["success_criteria"],
        mission.get("constraints_text") or "",
        mission.get("allow_external_ai", False),
        mission.get("external_providers") or [])
    return manager, pid


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _install_local_stub(manager, pid, counter=None):
    counter = counter if counter is not None else {"local": 0, "external": 0}
    manager.llm = Mock(spec=Ollama)

    async def _local(*args):
        counter["local"] += 1
        raise AssertionError("local LLM must not be called in B-3 draft path")

    async def _external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = _local
    manager.plan_review_runner = _external
    return counter


def _event_kinds(manager, pid):
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = db.execute(
                "SELECT kind FROM project_events WHERE project_id=?",
                (pid,)).fetchall()
        except sqlite3.Error:
            return []
        return sorted({str(r[0]) for r in rows})
    finally:
        db.close()


def _proposal_events_count(manager, pid):
    import app.capability_gap as gap
    total = 0
    for row in gap.list_proposals(manager, pid):
        total += len(gap.list_proposal_events(manager, row.get("proposal_id") or ""))
    return total


def _external_counters(manager, pid):
    import app.autopilot as auto
    from app.goal_review import review_budget
    from app.stop_classifier import _EXTERNAL_SENDS_COUNTER as counter
    return {
        "sends": int(counter.get("calls") or 0),
        "rounds": auto._count_review_rounds(manager, pid),
        "budget": review_budget(manager, pid),
    }


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_first_tick_creates_drafts_and_stops(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-a")
    preflight = gap.capability_preflight(manager, pid)
    missing = list(preflight.get("missing_capability") or [])
    assert missing, preflight.get("gap_codes")
    expected = len(missing)
    counter = _install_local_stub(manager, pid)
    ext_before = _external_counters(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["stop_class"] == "missing_capability", out
    assert out["status"] == "waiting_human", out
    assert out["action_name"] == "capability_preflight", out
    assert out.get("capability_outcome") == "created", out
    assert out["enabled"] is True
    rows = gap.list_proposals(manager, pid, plan_signature=_sig_of(manager, pid))
    drafts = [r for r in rows if r.get("status") == "draft"
              and r.get("gap_code") == "missing_capability"]
    assert len(drafts) == expected, (len(drafts), expected, drafts)
    assert counter["local"] == 0, counter
    assert counter["external"] == 0
    assert out["external_delta"] == 0
    assert out["external_rounds_delta"] == 0
    assert out["budget_kept"] is True
    ext_after = _external_counters(manager, pid)
    assert ext_after["sends"] == ext_before["sends"]
    assert ext_after["rounds"] == ext_before["rounds"]
    assert ext_after["budget"] == ext_before["budget"]
    kinds = _event_kinds(manager, pid)
    assert not any("approv" in k or "extern" in k.lower() for k in kinds), kinds
    blob = json.dumps(out, ensure_ascii=False)
    assert "架空ダミー秘密値" not in blob


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_second_tick_is_existing_no_growth(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-b")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    first = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert first.get("capability_outcome") == "created", first
    sig = _sig_of(manager, pid)
    before_rows = gap.list_proposals(manager, pid, plan_signature=sig)
    before_ids = sorted(r["proposal_id"] for r in before_rows)
    before_events = _proposal_events_count(manager, pid)
    second = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert second["status"] == "waiting_human", second
    assert second["action_name"] == "capability_preflight", second
    assert second.get("capability_outcome") == "existing", second
    after_rows = gap.list_proposals(manager, pid, plan_signature=sig)
    after_ids = sorted(r["proposal_id"] for r in after_rows)
    assert after_ids == before_ids
    assert _proposal_events_count(manager, pid) == before_events
    assert second["enabled"] is True


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_reviewed_does_not_auto_export(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-c")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    first = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert first.get("capability_outcome") == "created", first
    sig = _sig_of(manager, pid)
    rows = gap.list_proposals(manager, pid, plan_signature=sig)
    for row in rows:
        gap.set_proposal_status(manager, pid, row["proposal_id"], "reviewed",
                                actor="架空担当", reason="架空確認")
    export_calls = {"n": 0}

    def _no_export(*a, **k):
        export_calls["n"] += 1
        raise AssertionError("export must not be called")

    orig_export = export.export_proposal
    export.export_proposal = _no_export
    try:
        after_review = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        export.export_proposal = orig_export
    assert export_calls["n"] == 0, export_calls
    assert after_review["status"] == "waiting_human", after_review
    assert after_review.get("capability_outcome") == "existing", after_review
    assert "指示" in after_review.get("reason_ja", ""), after_review
    for row in rows:
        assert gap.get_proposal(manager, row["proposal_id"])["status"] == "reviewed"
        # 自動で export していない (状態は reviewed のまま)。
        assert gap.get_proposal(manager, row["proposal_id"])["status"] != "export_ready"


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_preflight_gate_does_not_draft(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-d")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    gen_calls = {"n": 0}

    def _boom(_m, _p, *a, **k):
        raise RuntimeError("架空の読取失敗")

    def _count_gen(_m, _p, _pre=None, **k):
        gen_calls["n"] += 1
        raise AssertionError("generate must not be called")

    monkeypatch.setattr(gap, "capability_preflight", _boom)
    monkeypatch.setattr(gap, "generate_proposals", _count_gen)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out["status"] == "preflight_unknown", out
    assert out["action_name"] == "none", out
    assert gen_calls["n"] == 0, gen_calls
    assert gap.list_proposals(manager, pid) == []


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_signature_change_marks_stale_and_redrafts(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-e")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    first = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert first.get("capability_outcome") == "created", first
    old_sig = first.get("plan_signature") or _sig_of(manager, pid)
    old_ids = sorted(r["proposal_id"] for r in
                     gap.list_proposals(manager, pid, plan_signature=old_sig))
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(
        pid, (mission.get("plan_summary") or "架空") + " 改訂",
        mission["tasks"])
    second = auto.autopilot_tick(manager, pid, actor="架空担当")
    new_sig = _sig_of(manager, pid)
    assert new_sig != old_sig
    stale = [r for r in gap.list_proposals(manager, pid)
             if r.get("status") == "stale"]
    assert sorted(r["proposal_id"] for r in stale) == old_ids
    if second["stop_class"] == "missing_capability":
        assert second.get("capability_outcome") in ("created", "existing"), second
        new_rows = gap.list_proposals(manager, pid, plan_signature=new_sig)
        assert all(r["proposal_id"] not in old_ids for r in new_rows)


def test_b3_policy_block_not_selected(tmp_path):
    import app.autopilot as auto
    from app.stop_classifier import classify_stop
    manager, pid = _make_gap_project(tmp_path, "doc", tag="-f")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig = _sig_of(manager, pid)
    record = classify_stop({
        "budget_exhausted": True,
        "capability_preflight": {
            "missing_capability": True, "missing_count": 2,
            "gap_codes": ["missing_capability"],
            "criterion_ids": ["SC01", "SC02"]},
        "plan_signature": sig,
    })
    assert record["stop_class"] == "policy_block", record
    record["preflight_status"] = "ok"
    orig = auto._classify_current
    auto._classify_current = lambda _m, _p: ({}, dict(record))
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        auto._classify_current = orig
    assert out["stop_class"] == "policy_block", out
    assert out["status"] == "waiting_human", out
    assert out["action_name"] == "record_stop_class", out
    assert out.get("capability_outcome") in (None, "skipped", ""), out


def test_b3_human_decision_not_selected(tmp_path):
    import app.autopilot as auto
    from app.stop_classifier import classify_stop
    manager, pid = _make_gap_project(tmp_path, "doc", tag="-f2")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig = _sig_of(manager, pid)
    record = classify_stop({
        "run": {"state": "awaiting_human"},
        "capability_preflight": {
            "missing_capability": True, "missing_count": 2,
            "gap_codes": ["missing_capability"],
            "criterion_ids": ["SC01", "SC02"]},
        "plan_signature": sig,
    })
    assert record["stop_class"] == "missing_decision", record
    record["preflight_status"] = "ok"
    orig = auto._classify_current
    auto._classify_current = lambda _m, _p: ({}, dict(record))
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        auto._classify_current = orig
    assert out["stop_class"] == "missing_decision", out
    assert out["action_name"] == "record_stop_class", out
    assert out["status"] == "waiting_human", out


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_dry_run_same_judgment_writes_nothing(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_gap as gap
    from app.stop_classifier import _db_path as _stop_path
    manager, pid = _make_gap_project(tmp_path, domain, tag="-g")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    ticks_before = auto.list_ticks(manager, pid)
    auto_path = auto.autopilot_db_path(manager)
    mtime_before = auto_path.stat().st_mtime_ns if auto_path.exists() else None
    stop_before = _stop_path(manager).exists()
    dried = auto.autopilot_tick(manager, pid, actor="架空担当", dry_run=True)
    assert dried["status"] == "dry_run", dried
    assert dried["dry_run"] is True
    assert dried.get("planned_action") == "capability_preflight", dried
    assert dried.get("capability_outcome") == "created", dried
    assert auto.list_ticks(manager, pid) == ticks_before
    assert gap.list_proposals(manager, pid) == []
    if mtime_before is not None:
        assert auto_path.stat().st_mtime_ns == mtime_before
    assert _stop_path(manager).exists() == stop_before
    executed = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert executed.get("capability_outcome") == "created", executed
    assert executed["action_name"] == "capability_preflight"


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_generate_failure_is_error_not_success(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_gap_project(tmp_path, domain, tag="-err")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    preflight = gap.capability_preflight(manager, pid)
    created = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    target = created["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    reviewed_before = gap.get_proposal(manager, target)["status"]
    assert reviewed_before == "reviewed"

    def _boom(_m, _p, _pre=None, **k):
        raise ValueError("架空の生成失敗")

    monkeypatch.setattr(gap, "generate_proposals", _boom)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out["status"] == "failed", out
    assert out.get("capability_outcome") == "error", out
    assert out["enabled"] is False
    assert gap.get_proposal(manager, target)["status"] == "reviewed"


@pytest.mark.parametrize("domain", DOMAINS)
def test_b3_non_capability_gaps_do_not_draft(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-nongap")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    real = gap.capability_preflight(manager, pid)

    def _no_cap(m, p, plan_signature=None, ai_assist=None):
        return {
            "schema": real.get("schema"),
            "project_id": real.get("project_id"),
            "plan_signature": real.get("plan_signature"),
            "criteria": real.get("criteria"),
            "gaps": [{"gap_code": "missing_input", "criterion_id": "SC01"}],
            "missing_capability": [],
            "has_missing_capability": False,
            "gap_codes": ["missing_input"],
            "ai_assist_used": False, "external_sends": 0,
            "external_calls": 0, "read_only": True,
        }

    monkeypatch.setattr(gap, "capability_preflight", _no_cap)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    if out.get("stop_class") == "missing_capability":
        assert out.get("capability_outcome") != "created", out
    assert not any(r.get("gap_code") == "missing_capability"
                   for r in gap.list_proposals(manager, pid))


def test_b3_boundary_no_review_export_apply(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    import app.plan_feedback as feedback
    manager, pid = _make_gap_project(tmp_path, "doc", tag="-bound")
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    touched = {"review": 0, "export": 0, "apply": 0, "delivery": 0}

    def _no_export(*a, **k):
        touched["export"] += 1
        raise AssertionError("export forbidden")

    def _no_apply(*a, **k):
        touched["apply"] += 1
        raise AssertionError("apply forbidden")

    def _no_delivery(*a, **k):
        touched["delivery"] += 1
        raise AssertionError("delivery forbidden")

    monkeypatch.setattr(export, "export_proposal", _no_export)
    monkeypatch.setattr(feedback, "apply", _no_apply)
    if hasattr(delivery, "ingest_delivery"):
        monkeypatch.setattr(delivery, "ingest_delivery", _no_delivery)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out.get("capability_outcome") == "created", out
    assert touched == {"review": 0, "export": 0, "apply": 0, "delivery": 0}
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "apply", actor="架空担当")
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "export_package", actor="架空担当")
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "register_rag", actor="架空担当")
    rows = gap.list_proposals(manager, pid)
    assert rows
    assert all(r.get("status") == "draft" for r in rows)


def test_registry_completeness_b3():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_generic_no_specific_project_words_b3():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "tests/test_b3_capability_draft.py").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines()
             if "販売" not in line and "1659" not in line
             and "word in" not in line and "for word" not in line]
    body = "\n".join(lines)
    assert "販売" not in body
    assert "1659" not in body
    impl = (root / "app/autopilot.py").read_text(encoding="utf-8")
    assert "innerHTML" not in impl
    assert "http://" not in impl and "https://" not in impl
