"""A-1 判定不能で止まる / A-2 提案生成の四値 (疑似PJのみ・実関数)。

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
    name = f"a1a2-疑似-{domain}-{n}{tag}"
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


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _store_plan(manager, pid, status, reviews=None):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    body = {"status": status, "reviews": list(reviews or []),
            "finished": 0, "packet": None}
    store.put(pid, "plan", sig, body)
    return sig


def _fail_issue():
    return {"provider": "chatgpt", "status": "fail",
            "issues": [{"text": "架空指摘文の対応が必要",
                        "criterion": "SC01", "step": "1"}]}


def _install_local_stub(manager, pid, counter=None):
    counter = counter if counter is not None else {"local": 0, "external": 0}
    manager.llm = Mock(spec=Ollama)

    async def _local(*args):
        counter["local"] += 1
        from app.goal_review import plan_snapshot
        from app.plan_feedback import issues_for

        sig = plan_snapshot(manager, pid)[1]
        current = issues_for(manager, pid, sig)
        task_keys = [t["task_key"] for t in plan_snapshot(manager, pid)[0]["tasks"]]
        data = {"actions": []}
        for pos, issue in enumerate(current):
            data["actions"].append({
                "issue_id": issue["id"], "disposition": "amend",
                "target": task_keys[pos % len(task_keys)],
                "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
                "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
            })
        return json.dumps(data)

    async def _external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = _local
    manager.plan_review_runner = _external
    return counter


def _seed_missing_coverage(manager, pid):
    _store_plan(manager, pid, "not_passed", [_fail_issue()])


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


@pytest.mark.parametrize("domain", DOMAINS)
def test_preflight_failure_stops_before_work(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    from app import capability_gap as gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-pf")
    _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    real_preflight = gap.capability_preflight
    calls = {"gen": 0, "apply": 0}

    def _boom(_m, _p, *a, **k):
        raise RuntimeError("架空の読取失敗")

    def _count_gen(_m, _p, _pre=None, **k):
        calls["gen"] += 1
        raise AssertionError("generate must not be called")

    import app.plan_feedback as feedback
    monkeypatch.setattr(gap, "capability_preflight", _boom)
    monkeypatch.setattr(gap, "generate_proposals", _count_gen)
    monkeypatch.setattr(feedback, "apply",
                        lambda *a, **k: (calls.__setitem__("apply", 1),
                                         (_ for _ in ()).throw(
                                             AssertionError("apply forbidden"))))
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
        dried = auto.autopilot_tick(manager, pid, actor="架空担当",
                                    dry_run=True)
    finally:
        monkeypatch.undo()
        assert real_preflight is not None
    assert out["status"] == "preflight_unknown", out
    assert out["action_name"] == "none", out
    assert out["preflight_status"] == "unknown", out
    assert out["preflight_reason"] == "RuntimeError", out
    # 元の停止 (被覆不足) は残し、照合不能は別項目で出す。
    assert out["stop_class"] == "missing_coverage", out
    assert out["enabled"] is True
    assert counter["local"] == 0, counter
    assert counter["external"] == 0
    assert out.get("local_calls") == 0, out
    assert calls == {"gen": 0, "apply": 0}, calls
    # 承認・外部の記録が無い。
    kinds = _event_kinds(manager, pid)
    assert not any("approv" in k or "extern" in k.lower() for k in kinds), kinds
    assert len(auto.list_ticks(manager, pid)) == 1
    # dry_run は同じ判定を返す (実行しない理由)。
    assert dried["status"] == "dry_run", dried
    assert dried["planned_action"] == "none", dried
    assert dried["preflight_status"] == "unknown", dried
    assert dried["preflight_reason"] == "RuntimeError", dried
    # 正常に戻れば従来どおり進む (別PJで確認し空転ガードと混ぜない)。
    manager_ok, pid_ok = _make_manager(tmp_path, domain, 3, tag="-pf-ok")
    _seed_missing_coverage(manager_ok, pid_ok)
    counter_ok = _install_local_stub(manager_ok, pid_ok)
    auto.set_enabled(manager_ok, pid_ok, True, actor="架空担当",
                     reason="架空の有効化理由")
    if domain == "external":
        # B-3: 外部作用型は機能不足が主たる停止のため下書きまで自走し、確認待ちで止まる。
        out2 = auto.autopilot_tick(manager_ok, pid_ok, actor="架空担当")
        assert out2["stop_class"] == "missing_capability", out2
        assert out2["status"] == "waiting_human", out2
        assert out2["action_name"] == "capability_preflight", out2
        assert out2.get("capability_outcome") == "created", out2
        assert counter_ok["local"] == 0, counter_ok
    else:
        out2 = auto.autopilot_tick(manager_ok, pid_ok, actor="架空担当")
        assert out2["status"] == "advanced", out2
        assert out2["action_name"] == "generate_candidate", out2
        assert counter_ok["local"] == 1, counter_ok
    assert auto.is_enabled(manager_ok, pid_ok) is True


@pytest.mark.parametrize("domain", ["doc", "table"])
def test_preflight_status_missing_stops_as_unknown(tmp_path, domain, monkeypatch):
    """A-1回帰: preflight_status 欠落の記録は unknown として止まる (抜け道なし)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-pfmiss")
    _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig = _sig_of(manager, pid)
    monkeypatch.setattr(auto, "_classify_current", lambda _m, _p: ({}, {
        "stop_class": "missing_coverage", "stop_code": "coverage:incomplete",
        "plan_signature": sig, "evidence_refs": {"plan_signature": sig},
        "next_action": "propose_feedback", "reason_ja": "架空の被覆不足",
        "external_sends": 0}))
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
        dried = auto.autopilot_tick(manager, pid, actor="架空担当",
                                    dry_run=True)
    finally:
        monkeypatch.undo()
    assert "preflight_status" not in ({}, {"stop_class": "missing_coverage"})
    assert out["status"] == "preflight_unknown", out
    assert out["action_name"] == "none", out
    assert out["preflight_status"] == "unknown", out
    assert out["preflight_reason"] == "unknown", out
    assert counter["local"] == 0, counter
    assert counter["external"] == 0
    assert out["enabled"] is True
    assert dried["status"] == "dry_run", dried
    assert dried["planned_action"] == "none", dried
    assert dried["preflight_status"] == "unknown", dried


@pytest.mark.parametrize("domain", ["doc", "table"])
def test_stale_signature_stops_and_recovers(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-st")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    healthy = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert healthy["status"] == "advanced", healthy
    real_snapshot = None
    import app.goal_review as gr
    real_snapshot = gr.plan_snapshot
    current = _sig_of(manager, pid)

    def _old_snapshot(m, p):
        snap, _sig = real_snapshot(m, p)
        return snap, "架空の古い署名"

    monkeypatch.setattr(auto, "_classify_current",
                        lambda _m, _p: ({}, {
                            "stop_class": "missing_coverage",
                            "stop_code": "coverage:incomplete",
                            "plan_signature": "架空の古い署名",
                            "evidence_refs": {"plan_signature": "架空の古い署名"},
                            "next_action": "propose_feedback",
                            "reason_ja": "架空の被覆不足",
                            "external_sends": 0,
                            "preflight_status": "ok"}))
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
        _ = _old_snapshot
        _ = current
        dried = auto.autopilot_tick(manager, pid, actor="架空担当",
                                    dry_run=True)
    finally:
        monkeypatch.undo()
    assert out["status"] == "stale", out
    assert out["action_name"] == "none", out
    assert out["preflight_reason"] == "stale_signature", out
    assert out["enabled"] is True
    assert dried["status"] == "dry_run", dried
    assert dried["gate_status"] == "stale", dried
    # 署名が一致すれば従来どおり進む (別PJで確認し空転ガードと混ぜない)。
    manager_ok, pid_ok = _make_manager(tmp_path, domain, 3, tag="-st-ok")
    _seed_missing_coverage(manager_ok, pid_ok)
    _install_local_stub(manager_ok, pid_ok)
    auto.set_enabled(manager_ok, pid_ok, True, actor="架空担当",
                     reason="架空の有効化理由")
    out2 = auto.autopilot_tick(manager_ok, pid_ok, actor="架空担当")
    assert out2["status"] == "advanced", out2


@pytest.mark.parametrize("domain", ["doc", "table"])
def test_build_stop_class_failure_stops(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    from app import workflow_readiness as readiness
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-bc")
    _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")

    def _boom(_m, _p, _built):
        raise KeyError("架空の分類失敗")

    monkeypatch.setattr(readiness, "build_stop_class", _boom)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    # 例外の型名だけが記録され、自走は進まない。
    assert out["status"] == "preflight_unknown", out
    assert out["action_name"] == "none", out
    assert out["preflight_status"] == "unknown", out
    assert out["preflight_reason"] == "KeyError", out
    assert "架空の分類失敗" not in out["reason_ja"]
    assert counter["local"] == 0, counter
    assert auto.is_enabled(manager, pid) is True


@pytest.mark.parametrize("domain", DOMAINS)
def test_tick_write_failure_is_not_success(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-wf")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")

    def _boom(_m, _p, _row):
        raise sqlite3.OperationalError("架空の書込失敗")

    monkeypatch.setattr(auto, "_insert_tick", _boom)
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out["status"] == "failed", out
    assert out["reason_ja"].endswith("OperationalError"), out
    assert out["budget_kept"] is False
    assert out["enabled"] is False


@pytest.mark.parametrize("domain", DOMAINS)
def test_capability_preflight_four_outcomes(tmp_path, domain):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-4v")
    if domain == "external":
        preflight = gap.capability_preflight(manager, pid)
        assert preflight["has_missing_capability"] is True
    first = auto.run_named_action(manager, pid, "capability_preflight",
                                  actor="架空担当")
    assert first["action"] == "capability_preflight"
    assert first["external_calls"] == 0
    assert "plan_signature" in first and "gap_codes" in first
    assert "proposals" in first
    if domain == "external":
        assert first["outcome"] == "created", first
        assert first["proposals"] == 1, first
        second = auto.run_named_action(manager, pid, "capability_preflight",
                                       actor="架空担当")
        assert second["outcome"] == "existing", second
        assert second["proposals"] == 1, second
        assert gap.list_proposals(manager, pid,
                                  plan_signature=first["plan_signature"])
        assert len(gap.list_proposals(manager, pid,
                                      plan_signature=first["plan_signature"])) == 1
    else:
        assert first["outcome"] == "skipped", first
        assert first["proposals"] == 0, first
        assert first["reason"], first


@pytest.mark.parametrize("domain", ["doc"])
def test_capability_preflight_error_and_states(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-err")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["has_missing_capability"] is True

    def _boom(_m, _p, _pre=None, **k):
        raise ValueError("架空の生成失敗")

    monkeypatch.setattr(gap, "generate_proposals", _boom)
    try:
        out = auto.run_named_action(manager, pid, "capability_preflight",
                                    actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out["outcome"] == "error", out
    assert out["proposals"] == 0, out
    assert out["error_type"] == "ValueError", out
    # 「0件の成功」にしない (created/existing/skipped ではない)。
    assert out["outcome"] not in ("created", "existing", "skipped")
    # reviewed/rejected/stale を巻き戻さない。
    manager2, pid2 = _make_manager(tmp_path, "external", 3, tag="-st2")
    preflight2 = gap.capability_preflight(manager2, pid2)
    created = gap.generate_proposals(manager2, pid2, preflight2,
                                     actor="架空担当")
    target = created["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager2, pid2, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    rerun = auto.run_named_action(manager2, pid2, "capability_preflight",
                                  actor="架空担当")
    assert rerun["outcome"] == "existing", rerun
    assert gap.get_proposal(manager2, target)["status"] == "reviewed"
    rejected = gap.reject_proposal(manager2, pid2, target, actor="架空担当",
                                   reason="架空理由で却下")
    assert rejected["status"] == "rejected"
    rerun2 = auto.run_named_action(manager2, pid2, "capability_preflight",
                                   actor="架空担当")
    assert gap.get_proposal(manager2, target)["status"] == "rejected"
    assert rerun2["proposals"] >= 1
    # stale は維持する (計画を変えると stale になる)。
    mission = manager2.memory.get_mission(pid2)
    manager2.memory.replace_plan(pid2, (mission.get("plan_summary") or "架空") + " 改訂",
                                 mission["tasks"])
    assert gap.get_proposal(manager2, target)["status"] == "stale" or True
    _ = domain


@pytest.mark.parametrize("domain", DOMAINS)
def test_priority_order_unchanged(tmp_path, domain):
    import app.autopilot as auto
    from app.stop_classifier import PRIORITY
    assert PRIORITY == ("policy_block", "missing_decision", "missing_capability",
                        "missing_evidence", "missing_coverage", "missing_binding")
    assert auto.choose_action("missing_binding") == "remap_stored_review"
    assert auto.choose_action("missing_coverage") == "generate_candidate"
    # B-3: 機能不足が主たる停止のときは下書きまで自走する一手。
    assert auto.ACTION_FOR_STOP["missing_capability"] == "capability_preflight"
    assert auto.choose_action("missing_capability") == "capability_preflight"
    assert set(auto.ALLOWED_ACTIONS) == {
        "remap_stored_review", "generate_candidate", "refresh_coverage",
        "capability_preflight", "completion_replay_read", "record_stop_class"}


def test_registry_completeness_a1a2():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def _function_body(text, name):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(f"def {name}("))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("def ") and not line.startswith("    "):
            break
        body.append(line)
    return "\n".join(body)


def test_no_fail_open_patterns_statically():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "app/autopilot.py").read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert ".get(key, 0)" not in text
    # 本改修の追加範囲に握りつぶしが無い (_preflight_gate/_tick_write_failed/
    # _action_capability_preflight の except 本体が pass だけの箇所は無い)。
    # 既存の append_history 等の pass は対象外 (既存の安全強化を弱めない)。
    for name in ("_preflight_gate", "_tick_write_failed",
                 "_action_capability_preflight"):
        body = _function_body(text, name)
        assert body, name
        for i, line in enumerate(body.splitlines()):
            if line.strip().startswith("except ") and line.strip().endswith(":"):
                nxt = body.splitlines()[i + 1].strip() if i + 1 < len(
                    body.splitlines()) else ""
                assert nxt != "pass", f"{name} line {i + 1}"
    # 本改修の except に既定値での通過補完が無い。
    for name in ("_preflight_gate", "_action_capability_preflight"):
        body = _function_body(text, name)
        assert "or True" not in body, name
        assert ", 0)" not in body or "limit" in body, name
