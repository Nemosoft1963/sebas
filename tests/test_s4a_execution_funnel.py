"""Stage 4-A: execution funnel (pseudo projects only).

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型/表データ型/外部作用型、架空)。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_plan, compile_task, contract_of
from app.workspace_files import WorkspaceSandbox
from app.execution_funnel import STAGES, project_funnel, portfolio_funnel

DOMAINS = ["doc", "table", "external"]
ROOT = Path(__file__).resolve().parents[1]
FILLER = "架空の検証可能な具体的内容を記載する。" * 40


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
    name = f"s4a-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tmp_tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        tmp_tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    plan = compile_plan(criteria, tmp_tasks, goal="架空目標-" + domain)
    memory.replace_plan(pid, plan["summary"], plan["tasks"])
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    from app.goal_contract import activate as _activate
    _activate(manager, pid)
    return manager, pid


def _make_shared_manager(tmp_path, domain, n, tag, db_name="s4a-shared.db"):
    assert "架空" in f"架空-{domain}"
    name = f"s4a-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / db_name
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tmp_tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        tmp_tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    plan = compile_plan(criteria, tmp_tasks, goal="架空目標-" + domain)
    memory.replace_plan(pid, plan["summary"], plan["tasks"])
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "ws-shared"),
    )
    from app.goal_contract import activate as _activate
    _activate(manager, pid)
    return manager, pid


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _write_markdown(manager, pid, path, headings):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"## {heading}\n{FILLER}" for heading in headings)
    target.write_text(body, encoding="utf-8")
    return target


def _write_output(manager, pid, output):
    path = output["path"]
    if path.endswith(".csv"):
        project = manager.memory.get_project(pid)
        target = manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid, path)[2]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(",".join(output["required_columns"]) + "\n架空1,2\n",
                          encoding="utf-8")
        return target
    headings = output.get("required_headings") or ["目的", "実施内容"]
    return _write_markdown(manager, pid, path, headings)


def _complete_outputs(manager, pid, task):
    contract = contract_of(task) or {}
    for output in contract.get("outputs") or []:
        _write_output(manager, pid, output)
    manager.memory.update_task(task["id"], "completed", result="成果物作成済み")


def _final_text(criteria_ids, verdicts=None):
    verdicts = verdicts or {cid: "PASS" for cid in criteria_ids}
    sections = ["# 最終検証・達成条件別の判定", "", "## 達成条件別判定"]
    for cid in criteria_ids:
        sections.append(f"### {cid} — {verdicts[cid]}")
        sections.append("")
        sections.append(f"- {cid}: {verdicts[cid]}")
        sections.append("- 根拠パス: result/")
        sections.append(FILLER)
    sections.extend(["", "## 成果物検証", FILLER, "", "## 未達条件と承認待ち", FILLER, ""])
    return "\n".join(sections)


def _complete_final(manager, pid, task, criteria_ids, verdicts=None):
    contract = contract_of(task) or {}
    path = contract["outputs"][0]["path"]
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_final_text(criteria_ids, verdicts=verdicts), encoding="utf-8")
    manager.memory.update_task(task["id"], "completed", result="最終検証済み")


def _criterion_ids(manager, pid):
    mission = manager.memory.get_mission(pid)
    return [cid for task in mission["tasks"]
            for cid in ((contract_of(task) or {}).get("criterion_ids") or [])]


def _store_plan(manager, pid, status, reviews):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    ReviewStore(manager.memory.path).put(pid, "plan", sig, {
        "status": status, "reviews": list(reviews), "finished": 0})
    return sig


def _pass_review(provider="chatgpt"):
    return {"provider": provider, "status": "pass", "issues": [],
            "response": {"ok": True}}


def _unverifiable_review(provider="chatgpt"):
    return {"provider": provider, "status": "unverifiable", "issues": [],
            "response": {"ok": True}}


def _approve_plan(manager, pid):
    # 最小限のテスト用ヘルパー: 人の承認記録 (mission状態) を作る。
    # 製品コードにテスト専用分岐は入れない。承認は既存の状態遷移で表す。
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")


def _execute_criteria(manager, pid, skip=(), verdicts=None, complete_fv=True):
    mission = manager.memory.get_mission(pid)
    ids = _criterion_ids(manager, pid)
    for task in mission["tasks"]:
        key = task["task_key"]
        if key in skip:
            continue
        if (contract_of(task) or {}).get("final_verification"):
            if complete_fv:
                _complete_final(manager, pid, task, ids, verdicts=verdicts)
            continue
        _complete_outputs(manager, pid, task)
    return manager.memory.get_mission(pid)


def _accept(manager, pid):
    from app.completion_gate import accept
    return accept(manager, pid, accepted_by="架空確認者")


def _gate_preview(manager, pid):
    from app.completion_gate import evaluate
    return evaluate(manager, pid, persist=False)


def _stage_of(out, name):
    stage = out[name]
    assert set(stage) == {"count", "total", "claimed_without_evidence",
                          "unknown", "evidence_refs"}
    return stage


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


def _workspace_files(manager, pid):
    project = manager.memory.get_project(pid)
    _, scope, _ = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result")
    if not scope.parent.exists():
        return {}
    root = scope.parent if scope.name == "result" else scope
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root.parent if root.name != pid else root).as_posix()] = (
                path.stat().st_size, path.stat().st_mtime_ns)
    return out


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_a_plan_not_passed(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-a")
    _store_plan(manager, pid, "not_passed", [
        {"provider": "chatgpt", "status": "fail",
         "issues": [{"text": "架空指摘"}], "response": {"ok": True}}])
    out = project_funnel(manager, pid)
    assert out["read_only"] is True
    assert out["external_sends"] == 0
    for name in STAGES:
        stage = _stage_of(out, name)
        assert stage["total"] == 3
        assert stage["count"] == 0
        assert stage["count"] <= stage["total"]
    # 単調性: 後段は前段を超えない。
    counts = [out[name]["count"] for name in STAGES]
    assert counts == sorted(counts, reverse=True)
    assert all(c == 0 for c in counts)
    # 停止は現行署名の計画評価に集まる。
    assert out["per_criterion"]
    assert all(r["last_stage"] == "" for r in out["per_criterion"])


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_b_passed_unapproved_and_structure_only(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-b")
    # (b) 合格・未承認: missionはplanningのまま。
    _store_plan(manager, pid, "passed", [_pass_review()])
    out = project_funnel(manager, pid)
    assert _stage_of(out, "plan_passed")["count"] == 3
    assert _stage_of(out, "plan_approved")["count"] == 0
    assert _stage_of(out, "task_executed")["count"] == 0
    # 構造合格のみ・外部unverifiableは合格に数えない。
    manager2, pid2 = _make_manager(tmp_path, domain, 3, tag="-b2")
    _store_plan(manager2, pid2, "passed", [_unverifiable_review()])
    out2 = project_funnel(manager2, pid2)
    assert _stage_of(out2, "plan_passed")["count"] == 0
    assert _stage_of(out2, "plan_passed")["claimed_without_evidence"] == 3


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_c_partial_execution(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-c")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    mission = manager.memory.get_mission(pid)
    first_sc = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    _complete_outputs(manager, pid, first_sc)
    out = project_funnel(manager, pid)
    assert _stage_of(out, "plan_passed")["count"] == 3
    assert _stage_of(out, "plan_approved")["count"] == 3
    executed = _stage_of(out, "task_executed")
    if domain == "external":
        # SC01は文書工程のため実行される。SC03(送信)は外部証拠なしで数えない。
        assert executed["count"] == 1
        assert executed["claimed_without_evidence"] >= 0
    else:
        assert executed["count"] == 1
    assert executed["count"] <= 3
    counts = [out[name]["count"] for name in STAGES]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_d_external_without_evidence_not_executed(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-d")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    # 全工程を完了状態にするが、外部工程の証拠は作らない。
    mission = manager.memory.get_mission(pid)
    for task in mission["tasks"]:
        if (contract_of(task) or {}).get("final_verification"):
            continue
        contract = contract_of(task) or {}
        for output in contract.get("outputs") or []:
            _write_output(manager, pid, output)
        manager.memory.update_task(task["id"], "completed", result="架空完了")
    out = project_funnel(manager, pid)
    executed = _stage_of(out, "task_executed")
    if domain == "external":
        # 外部作用の工程は外部証拠なしで実行済みに数えない。
        assert executed["count"] == 2
        assert executed["claimed_without_evidence"] == 1
    else:
        assert executed["count"] == 3
    # 最終検証が未完了のため証拠検証は0。
    assert _stage_of(out, "evidence_verified")["count"] == 0


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_e_evidence_verified_and_partial_achieved(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-e")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    mission = manager.memory.get_mission(pid)
    ids = _criterion_ids(manager, pid)
    for task in mission["tasks"]:
        if (contract_of(task) or {}).get("final_verification"):
            continue
        contract = contract_of(task) or {}
        for output in contract.get("outputs") or []:
            _write_output(manager, pid, output)
        manager.memory.update_task(task["id"], "completed", result="架空完了")
    from app.goal_contract import activate as _activate2
    _activate2(manager, pid)
    # 外部作用型は外部証拠を登録して実行段を通す。
    if domain == "external":
        action = manager.memory.create_action(pid, "manual", "架空の見込み客A", "初回案内を実施")
        manager.memory.update_action(pid, action["id"], "approved")
        manager.memory.update_action(
            pid, action["id"], "executed",
            evidence="2026-03-01 架空面談記録 TEST-1 実施結果: 検証済み 学習内容: 仮説を更新")
    # (e) 全工程の証拠検証済み: 最終検証を完了させる。
    mission = manager.memory.get_mission(pid)
    fv = next(t for t in mission["tasks"] if (contract_of(t) or {}).get("final_verification"))
    _complete_final(manager, pid, fv, ids)
    out = project_funnel(manager, pid)
    gate_preview = _gate_preview(manager, pid)
    gate_by_id = {r["criterion_id"]: r for r in gate_preview.get("criteria") or []}
    expected_pass = sum(1 for v in gate_by_id.values() if v.get("status") == "PASS")
    if domain == "external":
        # SC03(送信)は action kind が一致しない外部証拠のため実行段で止まる。
        # 達成は完了ゲート全体の achieved が真のときだけ数える。
        assert _stage_of(out, "task_executed")["count"] == 2
        assert _stage_of(out, "evidence_verified")["count"] == 2
        assert _stage_of(out, "criterion_achieved")["count"] == 0
        counts = [out[name]["count"] for name in STAGES]
        assert counts == sorted(counts, reverse=True)
        claimed_total = sum(out[name]["claimed_without_evidence"] for name in STAGES)
        assert claimed_total >= 1
        return
    assert _stage_of(out, "task_executed")["count"] == 3
    assert _stage_of(out, "evidence_verified")["count"] == 3
    assert _stage_of(out, "criterion_achieved")["count"] == 0
    assert gate_preview.get("achieved") is False
    # (f) 条件の一部だけ達成: SC03の成果物を消して未達にする。
    mission = manager.memory.get_mission(pid)
    sc03 = next(t for t in mission["tasks"] if t["task_key"] == "SC03")
    for output in (contract_of(sc03) or {}).get("outputs") or []:
        project = manager.memory.get_project(pid)
        target = manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid, output["path"])[2]
        if target.is_file():
            target.unlink()
    partial = project_funnel(manager, pid)
    partial_gate = _gate_preview(manager, pid)
    partial_by_id = {r["criterion_id"]: r for r in partial_gate.get("criteria") or []}
    assert partial_by_id["SC03"]["status"] != "PASS"
    assert _stage_of(partial, "evidence_verified")["count"] <= 2
    assert _stage_of(partial, "criterion_achieved")["count"] == 0
    # 人間承認で条件が達成になる (成果物を戻して全PASSに戻し承認)。
    for output in (contract_of(sc03) or {}).get("outputs") or []:
        _write_output(manager, pid, output)
    assert gate_preview.get("achieved") is False
    result = _accept(manager, pid)
    assert result["achieved"] is True
    out2 = project_funnel(manager, pid)
    assert _stage_of(out2, "criterion_achieved")["count"] == 3
    counts = [out2[name]["count"] for name in STAGES]
    assert counts == sorted(counts, reverse=True)
    assert all(r["last_stage"] == "criterion_achieved" for r in out2["per_criterion"])
    assert out2["stage_stop_summary"] == {}


@pytest.mark.parametrize("domain", DOMAINS)
def test_stage_g_completed_without_evidence_claimed(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-g")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    # (g) 状態は済だが証拠なし: SC01だけ完了状態・成果物なし。
    mission = manager.memory.get_mission(pid)
    first = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    manager.memory.update_task(first["id"], "completed", result="架空の自己申告")
    out = project_funnel(manager, pid)
    executed = _stage_of(out, "task_executed")
    assert executed["count"] == 0
    assert executed["claimed_without_evidence"] == 1
    counts = [out[name]["count"] for name in STAGES]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.parametrize("domain", DOMAINS)
def test_broken_sources_are_unknown_not_pass(tmp_path, domain):
    from app import execution_funnel as funnel_mod
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-broken")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    _execute_criteria(manager, pid)
    before = project_funnel(manager, pid)
    assert _stage_of(before, "task_executed")["count"] >= 1
    # DBが無い (読取元が壊れている) 場合は unknown で通過に数えない。
    mem_parent = manager.memory.path.parent
    reviews_path = mem_parent / "goal_reviews.sqlite3"
    completion_path = mem_parent / "goal_completion.sqlite3"
    reviews_backup = mem_parent / "goal_reviews.sqlite3.s4a-bak"
    completion_backup = mem_parent / "goal_completion.sqlite3.s4a-bak"
    if reviews_path.is_file():
        reviews_path.rename(reviews_backup)
    if completion_path.is_file():
        completion_path.rename(completion_backup)
    try:
        broken = project_funnel(manager, pid)
        assert _stage_of(broken, "plan_passed")["count"] == 0
        assert _stage_of(broken, "plan_passed")["unknown"] == 2
        assert _stage_of(broken, "criterion_achieved")["count"] == 0
        assert _stage_of(broken, "criterion_achieved")["unknown"] == 2
        assert broken["stage_stop_summary"].get("unknown", 0) >= 1
    finally:
        if reviews_backup.is_file():
            reviews_backup.rename(reviews_path)
        if completion_backup.is_file():
            completion_backup.rename(completion_path)
    # 形式不正の場合も握りつぶして合格にしない。
    if reviews_path.is_file():
        with open(reviews_path, "r+b") as stream:
            stream.seek(0)
            stream.write(b"S4A-CORRUPT")
    try:
        corrupt = project_funnel(manager, pid)
        assert _stage_of(corrupt, "plan_passed")["count"] == 0
        assert _stage_of(corrupt, "plan_passed")["unknown"] == 2
    finally:
        if reviews_backup.is_file():
            reviews_backup.rename(reviews_path)
        if completion_backup.is_file():
            completion_backup.rename(completion_path)
    assert funnel_mod.STAGES == ("plan_passed", "plan_approved", "task_executed",
                                 "evidence_verified", "criterion_achieved")


@pytest.mark.parametrize("domain", DOMAINS)
def test_deterministic_readonly_no_external(tmp_path, domain, monkeypatch):
    import app.execution_funnel as funnel_mod
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-ro")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    _execute_criteria(manager, pid)
    mem_before = _row_counts(manager.memory.path)
    ws_before = _workspace_files(manager, pid)
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    monkeypatch.setattr("app.external_ai.call_provider_with_metadata", _boom)
    first = project_funnel(manager, pid)
    second = project_funnel(manager, pid)
    assert first == second
    assert first["external_sends"] == 0
    assert calls["n"] == 0
    assert _row_counts(manager.memory.path) == mem_before
    assert _workspace_files(manager, pid) == ws_before
    assert "stop_classifier" in open(ROOT / "app" / "execution_funnel.py",
                                     encoding="utf-8").read()


@pytest.mark.parametrize("domain", DOMAINS)
def test_portfolio_excludes_deleted_and_isolated(tmp_path, domain):
    import app.project_delete as _del
    manager, pid = _make_shared_manager(tmp_path, domain, 2, "-pf1")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    manager2, pid2 = _make_shared_manager(tmp_path, domain, 2, "-pf2")
    _store_plan(manager2, pid2, "not_passed", [
        {"provider": "chatgpt", "status": "fail", "issues": [{"text": "架空指摘"}],
         "response": {"ok": True}}])
    # 同一DB内の2PJで集計し、他PJのデータが混ざらない。
    assert manager.memory.path == manager2.memory.path
    both = portfolio_funnel(manager)
    by_id = {r["project_id"]: r for r in both["rows"]}
    assert by_id[pid]["plan_passed"]["count"] == 2
    assert by_id[pid2]["plan_passed"]["count"] == 0
    assert both["total"]["plan_passed"]["count"] == 2
    assert both["total"]["plan_passed"]["total"] == 4
    # 論理削除中PJは除外される。
    _del._set_state(manager.memory.path, pid2, "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        filtered = portfolio_funnel(manager)
        ids = [r["project_id"] for r in filtered["rows"]]
        assert pid in ids
        assert pid2 not in ids
        assert filtered["total"]["plan_passed"]["total"] == 2
    finally:
        _del._set_state(manager.memory.path, pid2, "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)


def test_api_ok_notfound_deleted_and_no_secret(tmp_path, monkeypatch):
    import app.project_delete as _del
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-api")
    _store_plan(manager, pid, "passed", [_pass_review()])
    _approve_plan(manager, pid)
    _execute_criteria(manager, pid)
    manager.memory.add_context_file(
        pid, "架空秘密.md", "sk-架空ダミー秘密値-xyz", 10, b"dummy",
        "text/markdown", "markdown", "", "0" * 64, "upload")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    ok = client.get(f"/api/projects/{pid}/execution-funnel")
    assert ok.status_code == 200
    body = ok.json()
    assert body["read_only"] is True
    assert body["project_id"] == pid
    blob = json.dumps(body, ensure_ascii=False)
    assert "架空ダミー秘密値" not in blob
    assert "架空の検証可能な具体的内容" not in blob
    assert client.get("/api/projects/存在しないPJ/execution-funnel").status_code == 404
    all_ok = client.get("/api/execution-funnel")
    assert all_ok.status_code == 200
    assert all_ok.json()["read_only"] is True
    _del._set_state(manager.memory.path, pid, "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        assert client.get(f"/api/projects/{pid}/execution-funnel").status_code == 410
    finally:
        _del._set_state(manager.memory.path, pid, "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)


def test_no_new_innerhtml_and_counts():
    assert Path("app/static/workflow_readiness.js").read_text(
        encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/project_mission.js").read_text(
        encoding="utf-8").count("innerHTML") == 28


def test_generic_no_specific_project_words():
    text = (ROOT / "app" / "execution_funnel.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "s4a-疑似", "1698"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text


def test_registry_completeness_s4a():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_portfolio_omits_project_when_delete_state_unreadable(tmp_path):
    from app.project_delete import delete_db_path

    manager, project_id = _make_manager(tmp_path, "doc", 2,
                                        tag="-delete-state-unreadable")
    assert any(row["project_id"] == project_id
               for row in portfolio_funnel(manager)["rows"])
    path = delete_db_path(manager.memory.path)
    path.write_bytes(b"not a sqlite database")
    assert all(row["project_id"] != project_id
               for row in portfolio_funnel(manager)["rows"])
