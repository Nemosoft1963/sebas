"""Stage 0-3/0-4: atomic apply, local reevaluation, candidate UI wiring.

疑似PJ・実関数・LLMと外部AIはスタブで呼出回数を数える。外部通信なし。
特定PJ・件数・IDに依存しない一般的な機構のテスト。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.experience_store import canonical
from app.goal_review import ReviewStore, plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.plan_feedback import (
    apply,
    import_feedback,
    issues_for,
    local_reevaluate_applied_plan,
    propose,
)
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

SECRET_DUMMY = "sk-fake0123456789abcdef"


def _manager(tmp_path):
    memory = ShortTermMemory(tmp_path / "conversations.db")
    project = memory.create_project("s0b-pseudo", workspace_path="projects/s0b")
    pid = project["id"]
    memory.save_mission(
        pid,
        "架空目標",
        "架空の達成条件1の実行と検証",
        "",
        True,
        ["chatgpt"],
    )
    task = compile_task(1, "架空の達成条件1の実行と検証", {
        "title": "架空工程", "scope": "架空の成果物を作成し根拠を検証する。",
        "headings": ["目的", "実施内容"],
    }, [])
    memory.replace_plan(pid, "s0b pseudo plan", [task])
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "workspace"),
    )
    return manager, pid


def _feedback(manager, pid, sig, provider, text):
    return import_feedback(manager, pid, sig, provider, text)


def _local_ok(manager, pid, sig, counter=None):
    async def local(*args):
        if counter is not None:
            counter["local"] += 1
        current = issues_for(manager, pid, plan_snapshot(manager, pid)[1])
        task_key = plan_snapshot(manager, pid)[0]["tasks"][0]["task_key"]
        data = {"actions": []}
        for issue in current:
            data["actions"].append({
                "issue_id": issue["id"], "disposition": "amend", "target": task_key,
                "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
                "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
            })
        return json.dumps(data)
    return local


def _external_counter(counter):
    async def external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")
    return external


@pytest.mark.asyncio
async def test_apply_creates_new_signature_and_local_reevaluation_without_external(tmp_path):
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    budget_before = __import__("app.goal_review", fromlist=["review_budget"]).review_budget(manager, pid)
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    assert row["status"] == "draft"
    out = apply(manager, pid, sig, row["candidate_id"])
    assert out["status"] == "awaiting_review"
    assert out["signature"] != sig
    assert out["new_signature"] == out["signature"]
    assert out["external_sends"] == 0
    reevaluation = out["local_reevaluation"]
    assert reevaluation["passed"] is True
    assert reevaluation["remaining"] == []
    assert reevaluation["external_sends"] == 0
    assert reevaluation["external_calls"] == 0
    assert reevaluation["version"] == "local-reevaluation-v1"
    # 旧版が残り、新版ができた
    mission = manager.memory.get_mission(pid)
    assert mission["plan_version"] == version_before + 1
    store = ReviewStore(manager.memory.path)
    old_plan = store.get(pid, "applied_plan", sig)
    assert old_plan is not None
    assert old_plan["superseded_by"] == out["signature"]
    assert old_plan["plan_version"] == version_before
    stored = store.get(pid, "revision", sig)
    assert stored["status"] == "applied"
    assert stored["new_signature"] == out["signature"]
    assert stored["local_reevaluation"]["passed"] is True
    new_row = store.get(pid, "applied_revision", out["signature"])
    assert new_row["local_reevaluation"]["passed"] is True
    # 外部AI呼出0・外部評価ラウンド増分0・予算消費0
    assert counter["external"] == 0
    assert counter["local"] == 1
    budget_after = __import__("app.goal_review", fromlist=["review_budget"]).review_budget(manager, pid)
    assert budget_after == budget_before
    rounds_before = len((store.get(pid, "plan", sig) or {}).get("reviews") or [])
    rounds_after = len((store.get(pid, "plan", out["signature"]) or {}).get("reviews") or [])
    assert rounds_after == 0
    assert rounds_before == 0
    events = manager.memory.get_mission(pid).get("events") or []
    kinds = {e.get("kind") for e in events}
    assert "plan_feedback_applied" in kinds
    assert "plan_feedback_apply_reevaluated" in kinds
    blob = canonical({"events": [
        {"kind": e.get("kind"), "message": e.get("message"), "detail": e.get("detail")}
        for e in events]})
    assert "外部送信は行っていません" in blob or "external_sends_delta" in blob
    assert SECRET_DUMMY not in blob


@pytest.mark.asyncio
async def test_apply_rejects_uncovered_candidate_without_partial_version(tmp_path):
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    store = ReviewStore(manager.memory.path)
    from app.plan_coverage_matrix import build_coverage_matrix
    empty_matrix = build_coverage_matrix(
        issues=row.get("issues") or [], actions=row.get("actions") or [], changes=[],
        task_order=["SC00"],
    )
    uncovered = store.get(pid, "revision", sig) or {}
    uncovered.update(status="draft", changes=[], coverage_matrix=empty_matrix)
    store.put(pid, "revision", sig, uncovered)
    with pytest.raises(ValueError, match="被覆不足"):
        apply(manager, pid, sig, row["candidate_id"])
    assert manager.memory.get_mission(pid)["plan_version"] == version_before
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_apply_same_signature_is_rolled_back_with_reason(tmp_path):
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    # 適用で署名が変わらない場合(差分が実質変化なし)の分岐を直接検証する。
    # 実API経路では before==after の差分でも replace_plan が版を進めるため、
    # 署名不変の検出は _apply_mutate を差し替えた失敗注入で確認する。
    store = ReviewStore(manager.memory.path)
    import app.plan_feedback as feedback_module
    original_mutate = feedback_module._apply_mutate

    def _no_change(*args, **kwargs):
        return {'status': 'awaiting_review', 'signature': sig, 'task_id': None,
                'public_draft': '', 'lifecycle': 'revalidation_pending',
                'resume_from': 'public_packet'}

    feedback_module._apply_mutate = _no_change
    try:
        with pytest.raises(ValueError, match="署名が変わりませんでした"):
            apply(manager, pid, sig, row["candidate_id"])
    finally:
        feedback_module._apply_mutate = original_mutate
    assert manager.memory.get_mission(pid)["plan_version"] == version_before
    failed = store.get(pid, "revision", sig)
    assert failed["status"] == "failed"
    assert "署名が変わりませんでした" in failed["error"]
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_apply_local_reevaluation_failure_rolls_back_and_keeps_unresolved(tmp_path):
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    # 再評価を不合格にするため、達成条件IDを消す破壊的な execution_plan を差し替える。
    # validate は通るが criteria_retention で落ちる必要があるため、
    # ここでは再評価関数へ直接「消えた達成条件」を持つ新旧スナップショットを渡すのではなく、
    # 適用後の独立検査で落ちるよう、出力パスを重複させた計画へ差し替える。
    store = ReviewStore(manager.memory.path)
    broken = store.get(pid, "revision", sig) or {}
    assert broken["execution_plan"] is None
    # 再評価関数自体の不合格分岐。
    old_snapshot = plan_snapshot(manager, pid)[0]
    new_snapshot = {"tasks": [{"task_key": "SC01", "description": "x",
                               "acceptance_criteria": '{"schema":"local-cowork-plan/v1","criterion_ids":[],"outputs":[]}',
                               "depends_on": []}]}
    result = local_reevaluate_applied_plan(
        manager, pid, source_signature=sig, new_signature="new",
        candidate_id=row["candidate_id"], actions=row.get("actions") or [],
        issues=row.get("issues") or [], changes=row.get("changes") or [],
        old_snapshot=old_snapshot, new_snapshot=new_snapshot)
    assert result["passed"] is False
    assert result["remaining"]
    assert result["external_sends"] == 0
    # 実適用経路: 再評価不合格なら計画を適用前へ戻し、failed+残項目を残す。
    import app.plan_feedback as feedback_module
    original_reeval = feedback_module.local_reevaluate_applied_plan

    def _failing_reeval(*args, **kwargs):
        out = original_reeval(*args, **kwargs)
        failed = dict(out)
        failed["passed"] = False
        failed["remaining"] = ["criteria_retention:missing_SC01"]
        return failed

    feedback_module.local_reevaluate_applied_plan = _failing_reeval
    try:
        with pytest.raises(ValueError, match="ローカル再評価"):
            apply(manager, pid, sig, row["candidate_id"])
    finally:
        feedback_module.local_reevaluate_applied_plan = original_reeval
    assert manager.memory.get_mission(pid)["plan_version"] == version_before
    failed = store.get(pid, "revision", sig)
    assert failed["status"] == "failed"
    assert failed["local_reevaluation"]["passed"] is False
    assert "criteria_retention:missing_SC01" in (failed["local_reevaluation"].get("remaining") or [])
    carried = [item for item in (store.get(pid, "feedback", sig) or {}).get("issues") or []
               if item.get("origin") == "local_reevaluation"]
    assert carried
    assert all("残項目" in str(item.get("text") or "") for item in carried)
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_apply_idempotent_double_and_concurrent(tmp_path):
    from app.plan_feedback import _apply_replay
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    first = apply(manager, pid, sig, row["candidate_id"])
    assert first.get("replayed") is not True
    # 既存の公開挙動どおり二重適用は理由付きで拒否されるが、版は増えない。
    with pytest.raises(ValueError, match="反映対象の修正案がありません"):
        apply(manager, pid, sig, row["candidate_id"])
    assert manager.memory.get_mission(pid)["plan_version"] == version_before + 1
    # 保存済み結果の再利用(同一適用関数経路)は _apply_replay で確認する。
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig)
    replayed = _apply_replay(manager, pid, sig, stored)
    assert replayed.get("replayed") is True
    assert replayed["new_signature"] == first["new_signature"]
    assert replayed["local_reevaluation"]["passed"] is True
    assert manager.memory.get_mission(pid)["plan_version"] == version_before + 1

    # 同時適用: 新しい疑似PJで2回 gather しても版は1つだけ増える
    manager2, pid2 = _manager(tmp_path / "concurrent")
    manager2.memory = ShortTermMemory(tmp_path / "concurrent" / "conversations.db")
    project = manager2.memory.create_project("s0b-pseudo-c", workspace_path="projects/s0b-c")
    pid2 = project["id"]
    manager2.memory.save_mission(pid2, "架空目標", "架空の達成条件1の実行と検証", "", True, ["chatgpt"])
    task = compile_task(1, "架空の達成条件1の実行と検証", {
        "title": "架空工程", "scope": "架空の成果物を作成し根拠を検証する。",
        "headings": ["目的", "実施内容"],
    }, [])
    manager2.memory.replace_plan(pid2, "s0b pseudo plan", [task])
    manager2.workspace = WorkspaceSandbox(tmp_path / "workspace-c")
    manager2.llm = Mock(spec=Ollama)
    manager2.plan_review_runner = _external_counter(counter)
    sig2 = plan_snapshot(manager2, pid2)[1]
    _feedback(manager2, pid2, sig2, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager2._local_complete = _local_ok(manager2, pid2, sig2, counter)
    row2 = await propose(manager2, pid2, sig2)
    version2_before = manager2.memory.get_mission(pid2)["plan_version"]
    results = []
    errors = []

    async def _one():
        try:
            results.append(await asyncio.to_thread(apply, manager2, pid2, sig2, row2["candidate_id"]))
        except ValueError as exc:
            errors.append(str(exc))

    # 同時適用(asyncio.gather): 片方が成功し、もう片方は適用済みとして拒否される。版は1つだけ増える。
    await asyncio.gather(_one(), _one())
    assert len(results) == 1
    assert len(errors) == 1
    assert "反映対象の修正案がありません" in errors[0]
    assert manager2.memory.get_mission(pid2)["plan_version"] == version2_before + 1
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_no_automatic_external_resend_and_limit_kept(tmp_path):
    from app.goal_review import review_budget
    from app.plan_review_loop import VERIFICATION_LIMIT
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = await propose(manager, pid, sig)
    budget_before = review_budget(manager, pid)
    out = apply(manager, pid, sig, row["candidate_id"])
    assert out["external_sends"] == 0
    assert counter["external"] == 0
    assert review_budget(manager, pid) == budget_before
    assert VERIFICATION_LIMIT == 2
    # 人が許可した場合のみ既存の仕組みで可能。上限2回が維持される。
    from app.plan_review_loop import max_external_calls
    assert max_external_calls(1) == 2
    assert max_external_calls(2) == 4


def _pseudo_project(tmp_path, name, criteria, kinds):
    memory = ShortTermMemory(tmp_path / (name + ".db"))
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    memory.save_mission(pid, "架空目標-" + name, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, (text, kind) in enumerate(zip(criteria, kinds), 1):
        proposal = {"title": f"架空工程{i}", "scope": text + "の成果物を作成し根拠を検証する。",
                    "headings": ["目的", "実施内容"]}
        task = compile_task(i, text, proposal, [])
        if kind == "external":
            import json as _json
            from app.structured_planning import contract_of as _contract_of
            contract = _contract_of(task) or {}
            contract["execution_kind"] = "approved_customer_engagement"
            contract["action_requirements"] = [{"kind": "approved_customer_engagement",
                                                "minimum_executed": 1, "evidence_required": True}]
            contract["external_actions"] = "approval_required"
            task["acceptance_criteria"] = _json.dumps(contract, ensure_ascii=False)
        tasks.append(task)
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


@pytest.mark.asyncio
async def test_general_cases_document_table_external(tmp_path):
    cases = [
        ("s0b-doc", [
            "架空文書の作成と検証", "架空章立ての作成と検証", "架空根拠の作成と検証",
        ], ["document", "document", "document"]),
        ("s0b-table", [
            "架空表の作成と検証", "架空集計の作成と検証", "架空照合表の作成と検証",
            "架空差分表の作成と検証", "架空確定表の作成と検証",
        ], ["document"] * 5),
        ("s0b-mixed", [
            "架空文書の作成と検証", "架空顧客対応の実行と検証",
            "架空承認記録の作成と検証", "架空外部連絡の実行と検証",
        ], ["document", "external", "document", "external"]),
    ]
    for name, criteria, kinds in cases:
        manager, pid = _pseudo_project(tmp_path, name, criteria, kinds)
        manager.llm = Mock(spec=Ollama)
        counter = {"local": 0, "external": 0}
        sig = plan_snapshot(manager, pid)[1]
        snapshot = plan_snapshot(manager, pid)[0]
        order = [t["task_key"] for t in snapshot["tasks"]]
        # 外部作用型の工程への amend は development へ正規化されるため、
        # 混在ケースは文書工程だけを対象にする。
        amendable = [t for t in snapshot["tasks"]
                     if not (__import__("app.structured_planning", fromlist=["contract_of"]).contract_of(t) or {}).get("execution_kind")]
        assert amendable, name
        for index, issue_text in enumerate([
            "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。",
            "達成条件の検証手順が不足しています。具体的な検証手順を追加してください。",
            "成果物の見出しが不足しています。必須見出しを追加してください。",
        ][: len(criteria)]):
            _feedback(manager, pid, sig, f"Claude-{index}", issue_text + f"({name}-{index})")

        async def local(*args, _manager=manager, _amendable=amendable):
            current = issues_for(_manager, pid, plan_snapshot(_manager, pid)[1])
            data = {"actions": []}
            for pos, issue in enumerate(current):
                target = _amendable[pos % len(_amendable)]["task_key"]
                data["actions"].append({
                    "issue_id": issue["id"], "disposition": "amend", "target": target,
                    "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
                    "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
                })
            return json.dumps(data)

        manager._local_complete = local
        manager.plan_review_runner = _external_counter(counter)
        row = await propose(manager, pid, sig)
        assert row["status"] == "draft", name
        out = apply(manager, pid, sig, row["candidate_id"])
        assert out["local_reevaluation"]["passed"] is True, name
        assert out["external_sends"] == 0, name
        assert counter["external"] == 0, name


def test_ui_calls_existing_apply_and_no_new_innerhtml():
    from pathlib import Path
    js = Path("app/static/goal_review.js").read_text(encoding="utf-8")
    assert js.count("innerHTML") == 1
    assert "renderApplyResult" in js
    assert "新署名" in js
    assert "外部送信は行われていません" in js
    assert "textContent" in js
    # 画面は既存の適用関数(POST /goal-review/feedback/apply)を呼ぶ
    assert "action('/feedback/apply'" in js
    assert "/feedback/apply" in js
    import app.web as web
    import inspect
    source = inspect.getsource(web.apply_plan_feedback)
    assert "from app.plan_feedback import apply" in source
    assert "return apply(" in source


def test_no_secret_in_response_log_audit(tmp_path):
    import asyncio as _asyncio
    manager, pid = _manager(tmp_path)
    manager.llm = Mock(spec=Ollama)
    counter = {"local": 0, "external": 0}
    sig = plan_snapshot(manager, pid)[1]
    secret_text = "工程に原本と結果の照合が不足しています。" + SECRET_DUMMY + "原本の参照箇所と照合結果を対応付けてください。"
    _feedback(manager, pid, sig, "Claude", secret_text)
    manager._local_complete = _local_ok(manager, pid, sig, counter)
    manager.plan_review_runner = _external_counter(counter)
    row = _asyncio.new_event_loop().run_until_complete(propose(manager, pid, sig))
    assert row["status"] == "draft"
    out = apply(manager, pid, sig, row["candidate_id"])
    assert out["local_reevaluation"]["passed"] is True
    store = ReviewStore(manager.memory.path)
    # 応答・新しく書いた記録・監査イベントに秘密値が出ないこと。
    # 保存済み指摘(feedback/issues)自体は既存の保存挙動であり対象外。
    revision = dict(store.get(pid, "revision", sig) or {})
    revision.pop("issues", None)
    applied = dict(store.get(pid, "applied_plan", sig) or {})
    applied.pop("tasks", None)
    new_revision = dict(store.get(pid, "applied_revision", out["signature"]) or {})
    events = manager.memory.get_mission(pid).get("events") or []
    blob = canonical({"response": {k: out[k] for k in ("new_signature", "local_reevaluation", "external_sends")},
                      "revision": revision,
                      "applied_plan": applied,
                      "new_revision": new_revision,
                      "events": [{"kind": e.get("kind"), "message": e.get("message"),
                                  "detail": e.get("detail")} for e in events]})
    assert SECRET_DUMMY not in blob
    assert counter["external"] == 0


def test_registry_completeness_without_new_tables():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_local_reevaluation_pure_function_calls_no_external(tmp_path):
    manager, pid = _manager(tmp_path)
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    issues = issues_for(manager, pid, sig)
    snapshot = plan_snapshot(manager, pid)[0]
    task_key = snapshot["tasks"][0]["task_key"]
    actions = [{"issue_id": issues[0]["id"], "disposition": "amend", "target": task_key,
                "change": "x" * 30, "reason": "y" * 20}]
    changes = [{"target": task_key, "before": "a", "after": "b"}]
    calls = {"external": 0}
    manager.plan_review_runner = _external_counter(calls)
    result = local_reevaluate_applied_plan(
        manager, pid, source_signature=sig, new_signature="new",
        candidate_id="cand", actions=actions, issues=issues, changes=changes,
        old_snapshot=snapshot, new_snapshot=snapshot)
    assert result["checks"] == ["structure", "coverage", "dependencies",
                                "criteria_retention", "independent_checks"]
    assert calls["external"] == 0
    assert result["external_sends"] == 0
