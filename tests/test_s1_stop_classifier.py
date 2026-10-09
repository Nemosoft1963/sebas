"""Stage1: 停止分類器の受入 (疑似PJ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
"""
import sqlite3
from types import SimpleNamespace

import pytest

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
            out.append(f"架空営業管理表{i:02d}の作成と検算")
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
    name = f"s1-疑似-{domain}-{n}{tag}"
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


def _inputs_for(domain, sig, kind):
    base = {"plan_signature": sig, "domain": domain}
    if kind == "missing_binding":
        return {**base, "binding_state": "invalid_reference"}
    if kind == "missing_coverage":
        return {**base, "coverage": {"passed": False, "rows": [
            {"criterion_id": "SC01", "coverage": "uncovered"}]}}
    if kind == "missing_decision":
        return {**base, "run": {"state": "awaiting_human", "stop_kind": "",
                                "plan_signature": sig}}
    if kind == "missing_evidence":
        return {**base, "gate": {"reason_code": "ARTIFACT_MISSING",
                                 "criteria": [], "failed_criteria": ["SC01"]}}
    if kind == "missing_capability":
        return {**base, "disposition": "development"}
    if kind == "policy_block":
        return {**base, "unapproved_ocr": True}
    raise AssertionError(kind)


EXPECTED = {
    "missing_binding": ("missing_binding", "binding:invalid_reference"),
    "missing_coverage": ("missing_coverage", "coverage:incomplete"),
    "missing_decision": ("missing_decision", "run:awaiting_human"),
    "missing_evidence": ("missing_evidence", "gate:ARTIFACT_MISSING"),
    "missing_capability": ("missing_capability", "disposition:development"),
    "policy_block": ("policy_block", "policy:unapproved_ocr"),
}


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("kind", list(EXPECTED))
def test_classify_six_classes_across_domains(domain, kind):
    from app.stop_classifier import classify_stop
    expected_class, expected_code = EXPECTED[kind]
    inputs = _inputs_for(domain, f"架空署名-{domain}", kind)
    out = classify_stop(inputs)
    # 期待値は入力から導く (対応表の写像と一致すること)
    from app.stop_classifier import CLASS_TABLE
    assert out["stop_class"] == expected_class == CLASS_TABLE[expected_code]
    assert out["stop_code"] == expected_code
    assert out["external_sends"] == 0
    assert out["reason_ja"]
    if expected_class == "policy_block":
        assert out["next_action"] == "none"
    else:
        from app.workflow_readiness import IMPLEMENTED_ACTIONS
        assert out["next_action"] in set(IMPLEMENTED_ACTIONS) | {"none"}


@pytest.mark.parametrize("domain", DOMAINS)
def test_readiness_gate_run_inputs_classify(tmp_path, domain):
    from app.stop_classifier import classify_stop
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-real")
    from app.workflow_readiness import build_readiness
    from app.completion_gate import REASON_COVERAGE, REASON_HUMAN
    built = build_readiness(manager, pid)
    assert built["phase"]  # 実関数の結果を使う
    out = classify_stop({"readiness": {"phase": built["phase"]},
                         "plan_signature": built["plan_signature"]})
    assert out["stop_class"] in {
        "missing_binding", "missing_coverage", "missing_decision",
        "missing_evidence", "missing_capability", "policy_block"}
    gate_out = classify_stop({"gate": {"reason_code": REASON_COVERAGE,
                                       "criteria": [], "failed_criteria": ["SC01"]},
                              "plan_signature": built["plan_signature"]})
    assert gate_out["stop_class"] == "missing_coverage"
    human_out = classify_stop({"gate": {"reason_code": REASON_HUMAN,
                                        "criteria": [], "failed_criteria": []},
                               "plan_signature": built["plan_signature"]})
    assert human_out["stop_class"] == "missing_decision"
    run_out = classify_stop({"run": {"state": "stopped", "stop_kind": "human_required",
                                     "plan_signature": built["plan_signature"]}})
    assert run_out["stop_class"] == "missing_decision"


def test_policy_block_never_proposes_bypass():
    from app.stop_classifier import NEXT_ACTION_TABLE, STOP_CLASSES, classify_stop
    assert "policy_block" in STOP_CLASSES
    assert len(STOP_CLASSES) == 6
    for code, nxt in NEXT_ACTION_TABLE.items():
        if str(code).startswith("policy:"):
            assert nxt == "none"
    for inputs in ({"unapproved_ocr": True},
                   {"external_resend_prohibited": True},
                   {"implicit_public": True},
                   {"limit_reached": True},
                   {"budget_exhausted": True},
                   {"readiness": {"phase": "waiting_budget"}},
                   {"run": {"state": "stopped", "stop_kind": "connection_settings"}}):
        out = classify_stop(dict(inputs))
        assert out["stop_class"] == "policy_block"
        assert out["next_action"] == "none"
        assert "安全境界" in out["reason_ja"]


def test_unclassifiable_falls_to_missing_decision():
    from app.stop_classifier import classify_stop
    for inputs in ({}, {"phase": "架空の未来phase"}, {"gate_reason": "架空REASON"},
                   {"binding_state": "架空state"}, {"disposition": "架空dispo"},
                   {"cause": "架空cause"}, {"policy_code": "架空policy"},
                   {"ambiguous_stop": True},
                   {"readiness": {"phase": "issues_open"},
                    "gate": {"reason_code": "ARTIFACT_MISSING"},
                    "ambiguous_stop": True}):
        out = classify_stop(dict(inputs))
        assert out["stop_class"] == "missing_decision"
        assert out["stop_code"] == "unknown:unclassifiable" or out["stop_code"] == "unknown:empty"
        assert "分類不能" in out["reason_ja"]
        assert out["next_action"] == "none"


def test_priority_is_deterministic():
    from app.stop_classifier import PRIORITY, classify_stop
    assert PRIORITY == ("policy_block", "missing_decision", "missing_capability",
                        "missing_evidence", "missing_coverage", "missing_binding")
    combos = [
        ({"binding_state": "invalid_reference", "coverage_incomplete": True},
         "missing_coverage", "coverage:incomplete"),
        ({"coverage_incomplete": True, "gate": {"reason_code": "ARTIFACT_MISSING"}},
         "missing_evidence", "gate:ARTIFACT_MISSING"),
        ({"gate": {"reason_code": "ARTIFACT_MISSING"}, "disposition": "development"},
         "missing_capability", "disposition:development"),
        ({"disposition": "development", "run": {"state": "awaiting_human"}},
         "missing_decision", "run:awaiting_human"),
        ({"run": {"state": "awaiting_human"}, "unapproved_ocr": True},
         "policy_block", "policy:unapproved_ocr"),
        ({"binding_state": "invalid_reference", "unapproved_ocr": True},
         "policy_block", "policy:unapproved_ocr"),
    ]
    for inputs, cls, code in combos:
        first = classify_stop(dict(inputs))
        second = classify_stop(dict(inputs))
        assert first == second
        assert first["stop_class"] == cls
        assert first["stop_code"] == code
        assert isinstance(first["also"], list)


def test_table_covers_all_existing_values():
    from app import completion_gate as gate_mod
    from app import resolution_coordinator as coord_mod
    from app import plan_review_loop as loop_mod
    from app.stop_classifier import CLASS_TABLE, NEXT_ACTION_TABLE, REASON_JA_TABLE
    from app.workflow_readiness import PHASE_LABELS
    # PHASE_LABELS の全キー
    for key in PHASE_LABELS:
        assert "phase:" + key in CLASS_TABLE, key
    # REASON_* の全値
    reasons = [v for k, v in vars(gate_mod).items()
               if k.startswith("REASON_") and isinstance(v, str)]
    assert reasons
    for value in reasons:
        base = value.split(":")[0]
        assert "gate:" + base in CLASS_TABLE, value
    # 原因の9分類の全値
    assert len(coord_mod.CAUSES) == 9
    for cause in coord_mod.CAUSES:
        assert "cause:" + cause in CLASS_TABLE, cause
    # ランの状態・停止種別の全値
    for state in loop_mod.STATES:
        assert "run:" + state in CLASS_TABLE, state
    for kind in ("human_required", "connection_settings", "cancelled",
                 "legacy", "verification_limit"):
        assert "run:" + kind in CLASS_TABLE, kind
    # 対応表の全キーが次の一手と日本語理由を持つ
    for code in CLASS_TABLE:
        assert code in NEXT_ACTION_TABLE, code
        assert code in REASON_JA_TABLE, code


def test_next_action_only_implemented_or_none():
    from app.stop_classifier import NEXT_ACTION_TABLE, classify_stop
    from app.workflow_readiness import IMPLEMENTED_ACTIONS
    allowed = set(IMPLEMENTED_ACTIONS) | {"none"}
    for code, nxt in NEXT_ACTION_TABLE.items():
        assert nxt in allowed, code
    # 実分類の出力も同様
    out = classify_stop({"readiness": {"phase": "issues_open"}})
    assert out["next_action"] in allowed


def test_read_only_no_db_no_file_no_external(tmp_path):
    from app import stop_classifier as sc
    from app.stop_classifier import classify_stop
    from app.workflow_readiness import IMPLEMENTED_ACTIONS
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-ro")
    sig = _sig_of(manager, pid)

    def _row_counts(path):
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            counts = {}
            for table in tables:
                try:
                    counts[table] = int(db.execute(
                        f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                except sqlite3.Error:
                    continue
            return counts
        finally:
            db.close()

    mem_before = _row_counts(manager.memory.path)
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    from app.workflow_readiness import build_readiness
    built_before = build_readiness(manager, pid)
    inputs = {"readiness": {"phase": built_before["phase"], "plan_signature": sig},
              "plan_signature": sig,
              "gate": {"reason_code": "ARTIFACT_MISSING", "criteria": [],
                       "failed_criteria": ["SC01"]},
              "external_calls_before": 0, "external_calls_after": 0}
    out1 = classify_stop(dict(inputs))
    out2 = classify_stop(dict(inputs))
    assert out1 == out2
    assert out1["external_sends"] == 0
    assert calls["n"] == 0
    # モジュール内カウンタも増えていない (分類中に外部呼出なし)
    assert sc._EXTERNAL_SENDS_COUNTER["calls"] == 0
    # 秘密・本文全文を入れない
    secret_inputs = dict(inputs)
    secret_inputs["note"] = "架空の本文全文" + "あ" * 5000
    secret_inputs["password"] = "架空ダミー秘密値"
    secret_out = classify_stop(secret_inputs)
    blob = str(secret_out)
    assert "架空ダミー秘密値" not in blob
    assert "あ" * 100 not in blob
    # DB・ファイル不変 (classify_stop 自体は既存DBを読まない・書かない)
    mem_after = _row_counts(manager.memory.path)
    assert mem_before == mem_after
    assert set(IMPLEMENTED_ACTIONS)  # 参照のみ


def test_readiness_adds_stop_class_without_changing_keys(tmp_path):
    from app.workflow_readiness import build_readiness
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-keys")
    built = build_readiness(manager, pid)
    assert "stop_class" in built
    record = built["stop_class"]
    assert record["stop_class"] in {"missing_binding", "missing_coverage",
                                    "missing_decision", "missing_evidence",
                                    "missing_capability", "policy_block"}
    assert record["external_sends"] == 0
    # 既存キーが残っている (追加のみ)
    for key in ("phase", "state", "stop_reason", "next_action", "gate",
                "allowed_actions", "blocked_actions", "plan_signature"):
        assert key in built, key


def test_stop_class_endpoint_read_only(tmp_path):
    import asyncio
    from app import web as web_mod
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-api")
    web_mod.memory = manager.memory
    web_mod.orchestrator = manager
    web_mod.workspace = manager.workspace
    before_files = set(p.name for p in tmp_path.iterdir())
    first = asyncio.run(web_mod.get_stop_class(pid))
    second = asyncio.run(web_mod.get_stop_class(pid))
    assert first == second
    assert first["project_id"] == pid
    assert first["read_only"] is True
    assert first["stop_class"]["external_sends"] == 0
    # 履歴は GET では書かれない
    from app.stop_classifier import _db_path
    assert not _db_path(manager).exists()
    # build_readiness は goal_completion 等の既存DBを読むだけで、
    # tmp 直下の疑似PJファイル自体は変えない。既存DBの作成は副作用ではない。
    after_files = set(p.name for p in tmp_path.iterdir())
    assert before_files <= after_files
    assert "s1-疑似-external-3-api.db" in after_files


def test_idle_guard_two_consecutive_stops_run(tmp_path):
    import asyncio
    from app.plan_review_loop import get_run, start_run
    from app.stop_classifier import append_history, classify_stop, is_repeating
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-guard")
    sig = _sig_of(manager, pid)
    record = classify_stop({"run": {"state": "stopped", "stop_kind": "human_required",
                                    "plan_signature": sig},
                            "plan_signature": sig})
    flat = {"plan_signature": sig, "stop_class": record["stop_class"],
            "stop_code": record["stop_code"]}
    assert is_repeating([], flat) is False
    first = append_history(manager, pid, flat, actor="auto-tick", idempotency_key="k1")
    history = [first]
    assert is_repeating(history, flat) is True
    # 同一署名・同一コードの2連続でランが止まる
    run = {"id": "架空run", "project_id": pid, "state": "stopped",
           "stop_kind": "human_required", "plan_signature": sig,
           "verification_rounds": 1, "verification_limit": 2,
           "draft_mode": "local", "providers": []}

    async def _fake_locked(m, p, rid):
        from app.plan_review_loop import _stop_guard_tick
        guard, _ = _stop_guard_tick(m, p, run)
        assert guard["repeating"] is True
        return {"repeating": True}

    async def _fake_loop(m, p, rid):
        return await _fake_locked(m, p, rid)

    assert asyncio.run(_fake_loop(manager, pid, "x"))["repeating"] is True
    # 実の start_run は外部スタブ無しでは開始条件を満たさない (既存ゲート不変)
    with pytest.raises(ValueError):
        start_run(manager, pid, "架空者", ["chatgpt"])
    assert get_run if get_run else True


def test_guard_releases_on_new_signature_or_human(tmp_path):
    from app.stop_classifier import classify_stop, guard_auto_tick, is_repeating
    _m, _pid = _make_manager(tmp_path, "table", 3, tag="-rel")
    _m2, pid2 = _make_manager(tmp_path, "table", 3, tag="-rel2")
    sig_a = "架空署名A"
    sig_b = "架空署名B"
    rec_a = classify_stop({"run": {"state": "stopped", "stop_kind": "human_required",
                                   "plan_signature": sig_a},
                           "plan_signature": sig_a})
    flat_a = {"plan_signature": sig_a, "stop_class": rec_a["stop_class"],
              "stop_code": rec_a["stop_code"]}
    assert is_repeating([flat_a], dict(flat_a)) is True
    # 署名が変わると解除
    flat_b = dict(flat_a)
    flat_b["plan_signature"] = sig_b
    assert is_repeating([flat_a], flat_b) is False
    # 人の選択があると解除
    run = {"state": "stopped", "stop_kind": "human_required",
           "plan_signature": sig_a, "manual_bindings": [{"issue_id": "x"}]}
    guard = guard_auto_tick(_m2, pid2, run, rec_a)
    assert guard["repeating"] is False
    assert "解除" in guard["reason"]


def _pid_of(manager):
    # 疑似PJの先頭案件ID (入力から導く。固定IDを使わない)
    projects = manager.memory.list_projects()
    assert projects
    return projects[0]["id"]


def test_history_only_on_tick_and_idempotent(tmp_path):
    import asyncio
    from app.stop_classifier import append_history, classify_stop, read_history, _db_path
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-hist")
    sig = _sig_of(manager, pid)
    record = classify_stop({"run": {"state": "stopped", "plan_signature": sig},
                            "plan_signature": sig})
    flat = {"plan_signature": sig, "stop_class": record["stop_class"],
            "stop_code": record["stop_code"]}
    # GET では書かれない (read のみ)
    assert read_history(manager, pid) == []
    assert not _db_path(manager).exists()
    # ティックでだけ追記される
    first = append_history(manager, pid, flat, actor="auto-tick",
                           idempotency_key="tick:1")
    second = append_history(manager, pid, flat, actor="auto-tick",
                            idempotency_key="tick:1")
    assert first == second
    assert len(read_history(manager, pid)) == 1

    async def _concurrent():
        import asyncio as _aio
        return await _aio.gather(
            _aio.to_thread(append_history, manager, pid, flat,
                           "auto-tick", "tick:2"),
            _aio.to_thread(append_history, manager, pid, flat,
                           "auto-tick", "tick:2"))

    a, b = asyncio.run(_concurrent())
    assert a == b
    assert len(read_history(manager, pid)) == 2


def test_expectations_derived_and_no_secret(tmp_path):
    from app.stop_classifier import classify_stop
    for domain in DOMAINS:
        manager, pid = _make_manager(tmp_path, domain, 3, tag="-gen")
        sig = _sig_of(manager, pid)
        # 期待値は入力から導く: 被覆不足は missing_coverage、承認待ちは missing_decision
        cov = classify_stop({"coverage": {"passed": False, "rows": [
            {"criterion_id": f"SC-{domain}", "coverage": "uncovered"}]},
            "plan_signature": sig})
        assert cov["stop_class"] == "missing_coverage"
        dec = classify_stop({"run": {"state": "awaiting_human",
                                     "plan_signature": sig}})
        assert dec["stop_class"] == "missing_decision"
        out = classify_stop({"gate": {"reason_code": "ARTIFACT_MISSING"},
                             "plan_signature": sig,
                             "secret_dummy": "架空ダミー"})
        assert "架空ダミー" not in str(out["evidence_refs"])


def test_registry_completeness_s1():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1
