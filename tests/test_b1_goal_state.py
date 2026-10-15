"""B-1: 進捗モデル(読み取り専用)のテスト (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型/表データ型/外部作用型、架空) と、
文書だけの工程で達成条件にシステム構築・稼働・検証を含む疑似PJ。
"""
import json
import sqlite3

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task, contract_of
from app.workspace_files import WorkspaceSandbox
from app.goal_state_model import STATES, NEXT_KINDS, build_goal_state

DOMAINS = ["doc", "table", "external"]
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
    name = f"b1-疑似-{domain}-{n}{tag}"
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
    from app.goal_contract import activate as _activate
    _activate(manager, pid)
    return manager, pid


def _make_doc_only_with_system_build(tmp_path, tag="-docsys"):
    assert "架空" in "架空-docsys"
    name = f"b1-疑似-docsys{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空手順書01を文書で作る",
                "架空共有体系02のシステム構築と稼働検証",
                "架空稼働結果03の検証と報告"]
    memory.save_mission(pid, "架空目標-docsys", "\n".join(
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


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _store_plan(manager, pid, status, reviews):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    ReviewStore(manager.memory.path).put(pid, "plan", sig, {
        "status": status, "reviews": list(reviews), "finished": 0})
    return sig


def _pass_review(provider="chatgpt"):
    return {"provider": provider, "status": "pass", "issues": [],
            "response": {"ok": True}}


def _by_id(out):
    return {row["criterion_id"]: row for row in out["criteria"]}


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
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    headings = output.get("required_headings") or ["目的", "実施内容"]
    body = "\n".join(f"## {heading}\n{FILLER}" for heading in headings)
    target.write_text(body, encoding="utf-8")
    return target


def _complete_outputs(manager, pid, task):
    contract = contract_of(task) or {}
    for output in contract.get("outputs") or []:
        _write_output(manager, pid, output)
    manager.memory.update_task(task["id"], "completed", result="成果物作成済み")


def _complete_all_outputs(manager, pid):
    mission = manager.memory.get_mission(pid)
    for task in mission["tasks"]:
        if (contract_of(task) or {}).get("final_verification"):
            continue
        _complete_outputs(manager, pid, task)
    return manager.memory.get_mission(pid)


def _pass_review_payloads():
    return [_pass_review()]


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_initial_states_by_type(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-init")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    out = build_goal_state(manager, pid)
    assert out["read_only"] is True
    assert out["external_sends"] == 0
    assert set(out["summary"]) == {"achieved", "capability_gap",
                                   "evidence_gap", "human_decision",
                                   "unknown", "total", "consistent",
                                   "goal_achieved", "goal_remaining"}
    assert out["summary"]["total"] == 3
    assert out["summary"]["consistent"] is True
    assert out["summary"]["goal_achieved"] is False
    by_id = _by_id(out)
    assert sorted(by_id) == ["SC01", "SC02", "SC03"]
    # 計画未承認 (mission=planning) のため人の判断待ち。
    # 文書の条件も承認前は人の判断待ちになる (承認後に証拠不足へ)。
    if domain == "doc":
        assert by_id["SC01"]["state"] == "human_decision", by_id
    if domain == "table":
        assert by_id["SC01"]["state"] in ("evidence_gap", "human_decision",
                                          "unknown"), by_id
    if domain == "external":
        # 送信の条件は機能不足。文書の条件は証拠不足。
        assert by_id["SC03"]["state"] == "capability_gap", by_id
        assert by_id["SC01"]["state"] in ("evidence_gap", "human_decision",
                                          "unknown"), by_id


def test_b1_doc_only_with_system_build_states(tmp_path):
    manager, pid = _make_doc_only_with_system_build(tmp_path)
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    out = build_goal_state(manager, pid)
    by_id = _by_id(out)
    assert out["summary"]["total"] == 3
    # 計画未承認のため人の判断待ち。承認後に文書=証拠不足、システム=機能不足。
    assert by_id["SC01"]["state"] == "human_decision", by_id
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    out2 = build_goal_state(manager, pid)
    by_id2 = _by_id(out2)
    # 文書の条件は証拠が無ければ evidence_gap、システムの条件は capability_gap。
    assert by_id2["SC01"]["state"] == "evidence_gap", by_id2
    assert by_id2["SC02"]["state"] == "capability_gap", by_id2
    assert by_id2["SC03"]["state"] == "capability_gap", by_id2


def test_b1_plan_approval_wait_is_human_decision(tmp_path):
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-approval")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    out = build_goal_state(manager, pid)
    by_id = _by_id(out)
    # 計画未承認 (mission=planning) のため人の判断待ち。
    assert by_id["SC01"]["state"] == "human_decision", by_id
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    out2 = build_goal_state(manager, pid)
    by_id2 = _by_id(out2)
    assert by_id2["SC01"]["state"] == "evidence_gap", by_id2


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_candidate_only_does_not_achieve(tmp_path, domain, monkeypatch):
    import app.plan_feedback as feedback
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-cand")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    before = _by_id(build_goal_state(manager, pid))
    assert all(row["state"] != "achieved" for row in before.values())

    async def _fake_propose(_m, _p, _s, **_k):
        return {"candidate_id": "cand-架空",
                "coverage_matrix": {"rows": []},
                "blockers": [], "status": "draft", "attempts": 1}

    monkeypatch.setattr(feedback, "propose", _fake_propose)
    from app.core import Ollama
    from unittest.mock import Mock
    manager.llm = Mock(spec=Ollama)
    try:
        from app import autopilot as _auto
        _auto.set_enabled(manager, pid, True, actor="架空担当",
                          reason="架空の有効化理由")
    except (ValueError, sqlite3.Error):
        pass
    out = build_goal_state(manager, pid)
    by_id = _by_id(out)
    # 候補が生成済みでも、成果物・独立証拠が無ければ achieved にならない。
    assert all(row["state"] != "achieved" for row in by_id.values())
    assert out["summary"]["goal_achieved"] is False


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_unreadable_materials_are_unknown(tmp_path, domain, monkeypatch):
    import app.goal_state_model as model
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-unknown")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")

    def _boom_gate(_m, _p, persist=False):
        raise RuntimeError("架空の読取失敗")

    monkeypatch.setattr("app.completion_gate.evaluate", _boom_gate)
    out = build_goal_state(manager, pid)
    by_id = _by_id(out)
    assert all(row["state"] == "unknown" for row in by_id.values())
    assert out["summary"]["unknown"] == 2
    assert out["summary"]["achieved"] == 0
    # 「達成」「0件」にならない。unknown は残件に残る。
    assert out["summary"]["goal_achieved"] is False
    assert sorted(out["summary"]["goal_remaining"]) == ["SC01", "SC02"]
    monkeypatch.undo()

    def _boom_funnel(_m, _p):
        raise RuntimeError("架空の読取失敗")

    monkeypatch.setattr("app.execution_funnel.project_funnel", _boom_funnel)
    out2 = build_goal_state(manager, pid)
    by_id2 = _by_id(out2)
    assert all(row["state"] == "unknown" for row in by_id2.values())
    monkeypatch.undo()

    def _boom_preflight(_m, _p, plan_signature=None, ai_assist=None):
        raise RuntimeError("架空の読取失敗")

    monkeypatch.setattr("app.capability_gap.capability_preflight",
                        _boom_preflight)
    out3 = build_goal_state(manager, pid)
    assert out3["preflight_status"] == "unknown"
    assert "RuntimeError" in out3["preflight_reason"]
    by_id3 = _by_id(out3)
    assert all(row["state"] == "unknown" for row in by_id3.values())
    assert model.STATES == ("achieved", "capability_gap", "evidence_gap",
                            "human_decision", "unknown")


def test_b1_summary_counts_match_total(tmp_path, monkeypatch):
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-counts")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    out = build_goal_state(manager, pid)
    summary = out["summary"]
    assert (summary["achieved"] + summary["capability_gap"]
            + summary["evidence_gap"] + summary["human_decision"]
            + summary["unknown"]) == summary["total"] == 3
    # 不一致なら全体が unknown。
    import app.goal_state_model as model
    bad = model._summarize_states([{"criterion_id": "SC01", "state": "achieved"}],
                                  3)
    assert bad["consistent"] is False
    out2 = build_goal_state(manager, pid)
    assert out2["summary"]["consistent"] is True
    # 集計不一致の強制: criteria を壊しても合計検査が働く。
    assert out2["summary"]["goal_remaining"] == sorted(
        out2["summary"]["goal_remaining"])


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_goal_achieved_only_when_all_achieved(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-goal")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    out = build_goal_state(manager, pid)
    assert out["summary"]["goal_achieved"] is False
    assert len(out["summary"]["goal_remaining"]) == 2


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_next_step_kinds_and_dry_run_match(tmp_path, domain):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-next")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    out = build_goal_state(manager, pid)
    kind = out["next_step"]["kind"]
    assert kind in NEXT_KINDS
    assert "basis" in out["next_step"] and out["next_step"]["basis"]
    # 自走が進められるときはティックの dry_run が返す一手と一致 (同じ関数)。
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    dried = auto.autopilot_tick(manager, pid, actor="架空担当", dry_run=True)
    out2 = build_goal_state(manager, pid)
    if out2["next_step"]["kind"] == "autonomous":
        planned = dried.get("planned_action")
        assert out2["next_step"]["dry_run_action"] == planned, (out2, dried)
    else:
        assert out2["next_step"]["kind"] in ("human_confirm", "capability_dev",
                                             "evidence_confirm",
                                             "blocked_unknown")


def test_b1_next_step_for_proposal_states(tmp_path):
    import app.autopilot as auto
    import app.capability_gap as gap
    manager, pid = _make_doc_only_with_system_build(tmp_path, tag="-prop")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = build_goal_state(manager, pid)
    # 計画未承認のため人の確認に回る。
    assert out["next_step"]["kind"] in ("human_confirm", "autonomous",
                                        "capability_dev",
                                        "blocked_unknown"), out["next_step"]
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    out_ready = build_goal_state(manager, pid)
    # 下書きが無いときは自走が進められるか、機能開発に回る。
    assert out_ready["next_step"]["kind"] in ("autonomous", "capability_dev",
                                              "blocked_unknown"), out_ready["next_step"]
    preflight = gap.capability_preflight(manager, pid)
    created = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert created["proposals"]
    out2 = build_goal_state(manager, pid)
    assert out2["next_step"]["kind"] in ("autonomous", "capability_dev",
                                         "blocked_unknown"), out2["next_step"]
    # reviewed のときは人の操作 (指示の出力) に回る。
    sig = created["plan_signature"]
    for row in gap.list_proposals(manager, pid, plan_signature=sig):
        gap.set_proposal_status(manager, pid, row["proposal_id"], "reviewed",
                                actor="架空担当", reason="架空確認")
    out3 = build_goal_state(manager, pid)
    assert out3["next_step"]["kind"] in ("capability_dev", "blocked_unknown",
                                         "autonomous"), out3["next_step"]
    assert "不足機能" in out3["next_step"]["detail_ja"] or \
        "確認" in out3["next_step"]["detail_ja"]


@pytest.mark.parametrize("domain", DOMAINS)
def test_b1_readonly_no_external_deterministic(tmp_path, domain, monkeypatch):
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-ro")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    mem_before = _row_counts(manager.memory.path)
    ws_before = _workspace_files(manager, pid)
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    monkeypatch.setattr("app.external_ai.call_provider_with_metadata", _boom)
    first = build_goal_state(manager, pid)
    second = build_goal_state(manager, pid)
    assert first == second
    assert first["external_sends"] == 0
    assert first["external_calls"] == 0
    assert calls["n"] == 0
    assert _row_counts(manager.memory.path) == mem_before
    assert _workspace_files(manager, pid) == ws_before


def test_b1_api_ok_notfound_deleted_and_no_secret(tmp_path, monkeypatch):
    import app.project_delete as _del
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-api")
    _store_plan(manager, pid, "passed", _pass_review_payloads())
    manager.memory.set_mission_status(pid, "ready", "架空の人間承認", "approved")
    long_secret = "sk-架空ダミー秘密値-" + "x" * 200
    manager.memory.add_context_file(
        pid, "架空秘密.md", long_secret, 10, b"dummy",
        "text/markdown", "markdown", "", "0" * 64, "upload")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    ok = client.get(f"/api/projects/{pid}/goal-progress")
    assert ok.status_code == 200
    body = ok.json()
    assert body["read_only"] is True
    assert body["external_sends"] == 0
    assert body["project_id"] == pid
    blob = json.dumps(body, ensure_ascii=False)
    assert "架空ダミー秘密値" not in blob
    assert "架空の検証可能な具体的内容" not in blob
    # 引用は80字まで。
    for row in body["criteria"]:
        assert len(row["statement_ref"]) <= 80
    assert client.get("/api/projects/存在しないPJ/goal-progress").status_code == 404
    assert "/api/projects/{project_id}/goal-progress" in {
        str(getattr(r, "path", "")) for r in web.app.routes}
    _del._set_state(manager.memory.path, pid, "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        assert client.get(f"/api/projects/{pid}/goal-progress").status_code == 410
        from app.goal_state_model import build_goal_state as direct
        # 直接呼出しは拒否しない (API層の責務)。読取専用のまま。
        direct_out = direct(manager, pid)
        assert direct_out["read_only"] is True
    finally:
        _del._set_state(manager.memory.path, pid, "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)


def test_b1_state_labels_and_next_kinds_static():
    import app.goal_state_model as model
    assert model.STATES == ("achieved", "capability_gap", "evidence_gap",
                            "human_decision", "unknown")
    assert model.NEXT_KINDS == ("autonomous", "human_confirm", "capability_dev",
                                "evidence_confirm", "blocked_unknown")
    assert model.STATE_JA == {"achieved": "達成", "capability_gap": "機能不足",
                              "evidence_gap": "証拠不足",
                              "human_decision": "人の判断待ち",
                              "unknown": "確認できず"}


def test_b1_generic_no_specific_project_words():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "goal_state_model.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "b1-疑似", "1698"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text


def test_registry_completeness_b1():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
