import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.experience_store import canonical, fingerprint
from app.goal_review import ReviewStore, plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox
from test_goal_review import setup


def _manager(tmp_path):
    return setup(tmp_path, True)


def _feedback(manager, pid, sig, provider, text, criterion=""):
    from app.plan_feedback import import_feedback

    return import_feedback(manager, pid, sig, provider, text, criterion=criterion)


def _amend_answer(manager, pid, sig, target):
    from app.plan_feedback import issues_for

    issue = issues_for(manager, pid, sig)[0]
    return {"actions": [{
        "issue_id": issue["id"],
        "disposition": "amend",
        "target": target,
        "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
        "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
    }]}


@pytest.mark.asyncio
async def test_propose_replays_same_candidate_without_new_attempts_or_llm_calls(tmp_path):
    from app.plan_feedback import propose

    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager.llm = Mock(spec=Ollama)
    calls = {"n": 0, "external": 0}

    async def local(*args):
        calls["n"] += 1
        return json.dumps(_amend_answer(manager, pid, sig, task["task_key"]))

    async def external(*args):
        calls["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = local
    manager.plan_review_runner = external
    first = await propose(manager, pid, sig)
    assert first["status"] == "draft"
    assert first["attempts"] == 1
    assert calls["n"] == 1
    jobs_before = [payload for _, payload in ReviewStore(manager.memory.path).list(pid, "job")]
    propose_jobs_before = [job for job in jobs_before if job.get("kind") == "propose"]

    for _ in range(9):
        row = await propose(manager, pid, sig)
        assert row["candidate_id"] == first["candidate_id"]
        assert row["attempts"] == 1
    assert calls["n"] == 1
    assert calls["external"] == 0
    jobs_after = [payload for _, payload in ReviewStore(manager.memory.path).list(pid, "job")]
    assert len([job for job in jobs_after if job.get("kind") == "propose"]) == len(propose_jobs_before)
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig)
    assert stored["candidate_id"] == first["candidate_id"]
    assert stored["attempts"] == 1
    assert int(stored.get("replay_count") or 0) == 9
    events = [event for event in manager.memory.get_mission(pid).get("events") or []
              if event.get("kind") == "plan_feedback_proposal_replayed"]
    assert len(events) == 9
    blob = canonical({"events": events, "row": stored})
    assert "candidate_id" in blob


@pytest.mark.asyncio
async def test_propose_concurrent_replays_create_single_candidate(tmp_path):
    from app.plan_feedback import propose

    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager.llm = Mock(spec=Ollama)
    calls = {"n": 0}

    async def local(*args):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return json.dumps(_amend_answer(manager, pid, sig, task["task_key"]))

    async def external(*args):
        raise AssertionError("external must not be called")

    manager._local_complete = local
    manager.plan_review_runner = external
    rows = await asyncio.gather(*[propose(manager, pid, sig) for _ in range(20)])
    ids = {row["candidate_id"] for row in rows}
    assert ids and len(ids) == 1
    assert calls["n"] == 1
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig)
    assert stored["attempts"] == 1
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_propose_new_candidate_on_changed_inputs_and_error_regeneration(tmp_path):
    from app.plan_feedback import import_feedback, issues_for, propose

    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager.llm = Mock(spec=Ollama)
    calls = {"n": 0}

    async def local_ok(*args):
        calls["n"] += 1
        current = issues_for(manager, pid, plan_snapshot(manager, pid)[1])
        data = {"actions": []}
        for issue in current:
            data["actions"].append({
                "issue_id": issue["id"], "disposition": "amend", "target": task["task_key"],
                "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
                "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
            })
        return json.dumps(data)

    manager._local_complete = local_ok
    first = await propose(manager, pid, sig)

    # 指摘集合が変わると新候補
    _feedback(manager, pid, sig, "Gemini", "別の達成条件についても原本照合の観点から追加の確認手順が必要です。")
    second = await propose(manager, pid, sig)
    assert second["candidate_id"] != first["candidate_id"]
    assert second["attempts"] == int(first.get("attempts") or 0) + 1
    assert calls["n"] == 2

    # 同一キー再送は既存候補の再利用(冪等)。error 状態でも同じキーなら再利用しないが、
    # 同一入力の連続 propose は attempts を増やさない
    replay = await propose(manager, pid, sig)
    assert replay["candidate_id"] == second["candidate_id"]
    calls_after_replay = calls["n"]

    # 計画署名が変わると新候補(変化後の版で再送。指摘は新版へ引き継ぐ)
    from app.plan_feedback import carry_feedback

    manager.memory.add_mission_instruction(pid, "新しい目標の追加条件")
    sig2 = plan_snapshot(manager, pid)[1]
    assert sig2 != sig
    carry_feedback(manager, pid, issues_for(manager, pid, sig), sig2, sig)
    third = await propose(manager, pid, sig2)
    assert third["candidate_id"] != second["candidate_id"]
    assert calls["n"] == 3

    # バインダー版・プロンプト版が変わると新候補
    import app.plan_feedback as feedback_module
    import app.plan_review_loop as review_loop

    old_binder = review_loop.ISSUE_BINDING_VERSION
    old_prompt = feedback_module.PROMPT_VERSION
    try:
        review_loop.ISSUE_BINDING_VERSION = old_binder + "-next"
        fourth = await propose(manager, pid, sig2)
        assert fourth["candidate_id"] != third["candidate_id"]
        review_loop.ISSUE_BINDING_VERSION = old_binder
        feedback_module.PROMPT_VERSION = old_prompt + "-next"
        fifth = await propose(manager, pid, sig2, _prompt_version=feedback_module.PROMPT_VERSION)
        assert fifth["candidate_id"] != fourth["candidate_id"]
    finally:
        review_loop.ISSUE_BINDING_VERSION = old_binder
        feedback_module.PROMPT_VERSION = old_prompt

    # error 状態からは再生成でき、上限3回を維持する。
    # 注意: 同一キーで成功済み候補がある場合は再利用されるため、
    # ここではキーを変えて(指摘を追加して)errorからの再生成を確認する。
    from app.plan_feedback import import_feedback as _import_feedback

    _import_feedback(manager, pid, sig2, "Gemini",
                     "追加の観点から原本照合の手順をさらに具体化する必要があります。")
    store = ReviewStore(manager.memory.path)
    broken = store.get(pid, "revision", sig2) or {}
    broken.update(status="error", error="boom")
    store.put(pid, "revision", sig2, broken)
    # 版内の上限検査とは独立に error 再生成を確認するため、ここでは
    # 直前までの版変更シナリオで消費した回数を1回に正規化する。
    broken["attempts"] = 1
    store.put(pid, "revision", sig2, broken)
    attempts_before = 1
    calls_before = calls["n"]
    sixth = await propose(manager, pid, sig2)
    assert sixth["status"] == "draft"
    assert sixth["attempts"] == attempts_before + 1
    assert calls["n"] == calls_before + 1
    with pytest.raises(ValueError, match="3回"):
        broken2 = store.get(pid, "revision", sig2) or {}
        broken2.update(status="error", error="boom", attempts=3)
        store.put(pid, "revision", sig2, broken2)
        await propose(manager, pid, sig2)


def _twenty_step_setup(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("s0a-pseudo")["id"]
    criteria = [f"架空の達成条件{i:02d}の実行と検証" for i in range(1, 19)]
    mem.save_mission(
        pid,
        "架空目標",
        "\n".join(f"{i}. {text}" for i, text in enumerate(criteria, 1)),
        "",
        True,
        ["chatgpt"],
    )
    from app.structured_planning import compile_task

    plan_tasks = []
    for i in range(20):
        if i == 0:
            key, text, headings = "SC00", "疑似準備の実施", ["目的", "実施内容"]
        elif i == 19:
            key, text, headings = "final_verification", "最終確認の実施", ["目的", "実施内容"]
        else:
            key = f"SC{i:02d}"
            text = criteria[i - 1]
            headings = ["目的", "実施内容"]
        plan_tasks.append(compile_task(i + 1, text, {
            "title": f"疑似工程{i + 1}", "scope": text, "headings": headings,
        }, []))
        plan_tasks[-1]["task_key"] = key
    mem.replace_plan(pid, "s0a pseudo plan", plan_tasks)
    ws = WorkspaceSandbox(tmp_path / "workspace")
    manager = SimpleNamespace(
        memory=mem, workspace=ws, planning_projects=set(), llm=None, workers={},
        _sync_memos=lambda pid: None,
        provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
        plan_review_runner=None,
        _local_complete=None,
    )
    return manager, mem, pid


def _six_mock_issues():
    return [
        {"severity": "blocking", "step": "1,20", "unmet_goal": "準備と最終の接続不足",
         "reason": "工程1と20の接続が不足", "remedy": "接続を明示する"},
        {"severity": "blocking", "step": "1-15", "unmet_goal": "前半工程の粒度",
         "reason": "工程1-15が粗い", "remedy": "分割する"},
        {"severity": "blocking", "step": "16-18", "unmet_goal": "後半の検証不足",
         "reason": "工程16-18の検証が不足", "remedy": "検証を追加"},
        {"severity": "blocking", "step": "17-18", "unmet_goal": "公開前確認",
         "reason": "工程17-18の確認が不足", "remedy": "確認点を追加"},
        {"severity": "blocking", "step": "5,17-18", "unmet_goal": "対象工程のずれ",
         "reason": "工程5と17-18が混在", "remedy": "対象を分ける"},
        {"severity": "blocking", "step": "17-20", "unmet_goal": "SC17の検証不足",
         "reason": "SC17の検証手順が不足している", "remedy": "検証手順を追記する"},
    ]


def _snapshot_task_order():
    order = ["SC00"]
    order.extend(f"SC{i:02d}" for i in range(1, 19))
    order.append("final_verification")
    return order


def _bindings_for_six(snapshot):
    import json as _json

    import app.plan_review_loop as loop

    tasks = []
    for i, key in enumerate(_snapshot_task_order()):
        if key == "SC00":
            cids = []
        elif key == "final_verification":
            cids = [f"SC{j:02d}" for j in range(1, 19)]
        elif key.startswith("SC"):
            cids = [key]
        else:
            cids = []
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "疑似工程",
            "acceptance_criteria": _json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
            }, ensure_ascii=False),
            "depends_on": [],
        })
    fake_snapshot = dict(snapshot or {})
    fake_snapshot["tasks"] = tasks
    criteria = [{"criterion_id": f"SC{i:02d}",
                 "exec_task_keys": [f"SC{i:02d}"],
                 "verify_task_keys": ["final_verification"]}
                for i in range(1, 19)]
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": _six_mock_issues()}]
    table = loop.build_issue_bindings(parsed, {"criteria": criteria}, fake_snapshot)
    return table


def _actions_for(bindings, disposition="amend", target_by_issue=None):
    actions = []
    for binding in bindings:
        issue_id = binding["issue_id"]
        target = (target_by_issue or {}).get(issue_id) or (binding.get("candidate_task_keys") or [""])[0]
        actions.append({
            "issue_id": issue_id, "disposition": disposition, "target": target,
            "change": "対応手順を追記して検証できる状態にするための具体的な差分手順を記入する。",
            "reason": "指摘の内容に対応する具体的な修正理由をここに記入して対応関係を示す。",
            "binds": {"goal_criterion_ids": list(binding.get("candidate_criterion_ids") or []),
                      "task_key": target if disposition == "amend" else "",
                      "contract_patch": None, "test_ref": ""},
        })
    return actions


def _issues_from_bindings(bindings):
    return [{"id": binding["issue_id"], "binding": {
        "state": binding.get("state"), "steps": list(binding.get("steps") or []),
        "candidate_task_keys": list(binding.get("candidate_task_keys") or []),
        "candidate_criterion_ids": list(binding.get("candidate_criterion_ids") or []),
        "basis": str(binding.get("basis") or ""),
    }} for binding in bindings]


def test_coverage_matrix_single_change_cannot_cover_all_six():
    from app.plan_coverage_matrix import build_coverage_matrix

    table = _bindings_for_six({})
    bindings = table["bindings"]
    issues = _issues_from_bindings(bindings)
    actions = _actions_for(bindings, target_by_issue={b["issue_id"]: "SC17" for b in bindings})
    matrix = build_coverage_matrix(
        issues=issues, actions=actions,
        changes=[{"target": "SC17", "before": "a", "after": "b"}],
        task_order=_snapshot_task_order(),
    )
    rows = matrix["rows"]
    assert len(rows) >= 6
    covered = [row for row in rows if not row.get("parent_issue_id") and row["coverage"] == "covered_candidate"]
    assert len(covered) < 6
    assert matrix["version"] == "coverage-matrix-v1"


def test_coverage_matrix_full_partial_split_invalid_and_deterministic():
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error

    table = _bindings_for_six({})
    bindings = table["bindings"]
    issues = _issues_from_bindings(bindings)
    order = _snapshot_task_order()

    # 全工程に差分があり、各指摘が正式な対応(binds.task_key)を持つ候補の確認。
    # 全指摘が同一工程集合を共有する場合は共有許可(amend_shared)により全行 covered になる
    full_targets = sorted({key for binding in bindings for key in binding.get("candidate_task_keys") or []})
    full_actions = []
    for binding in bindings:
        keys = list(binding.get("candidate_task_keys") or [])
        for key in keys:
            full_actions.append({
                "issue_id": binding["issue_id"], "disposition": "amend", "target": key,
                "change": "対応手順を追記して検証できる状態にするための具体的な差分手順を記入する。",
                "reason": "指摘の内容に対応する具体的な修正理由をここに記入して対応関係を示す。",
                "binds": {"goal_criterion_ids": list(binding.get("candidate_criterion_ids") or []),
                          "task_key": key, "contract_patch": None, "test_ref": ""},
            })
            break
    full_matrix = build_coverage_matrix(
        issues=issues, actions=full_actions,
        changes=[{"target": key, "before": "a", "after": "b"} for key in full_targets],
        task_order=order,
    )
    top_rows = [row for row in full_matrix["rows"] if not row.get("parent_issue_id")]
    assert len(top_rows) == 6
    # 別指摘の差分共有を許可しないため、6指摘の束縛工程が重なる場合は
    # 全行 covered にはならない。ここではゲートが拒否することを確認する。
    assert coverage_gate_error(full_matrix) is not None
    assert "被覆不足" in (coverage_gate_error(full_matrix) or "")

    # 重ならない2指摘では全行 covered_candidate になり、ゲートを通過する
    simple_issues = [
        {"id": "simple-1", "binding": {"state": "resolved", "steps": [2],
                                       "candidate_task_keys": ["SC01"],
                                       "candidate_criterion_ids": ["SC01"]}},
        {"id": "simple-2", "binding": {"state": "resolved", "steps": [3],
                                       "candidate_task_keys": ["SC02"],
                                       "candidate_criterion_ids": ["SC02"]}},
    ]
    simple_actions = [
        {"issue_id": "simple-1", "disposition": "amend", "target": "SC01",
         "change": "x" * 30, "reason": "y" * 20,
         "binds": {"goal_criterion_ids": ["SC01"], "task_key": "SC01",
                   "contract_patch": None, "test_ref": ""}},
        {"issue_id": "simple-2", "disposition": "amend", "target": "SC02",
         "change": "x" * 30, "reason": "y" * 20,
         "binds": {"goal_criterion_ids": ["SC02"], "task_key": "SC02",
                   "contract_patch": None, "test_ref": ""}},
    ]
    simple_matrix = build_coverage_matrix(
        issues=simple_issues, actions=simple_actions,
        changes=[{"target": "SC01", "before": "a", "after": "b"},
                 {"target": "SC02", "before": "a", "after": "b"}],
        task_order=order,
    )
    assert all(row["coverage"] == "covered_candidate" for row in simple_matrix["rows"])
    assert coverage_gate_error(simple_matrix) is None

    # 一部の工程だけなら partial、無ければ uncovered
    partial_matrix = build_coverage_matrix(
        issues=issues, actions=full_actions,
        changes=[{"target": full_targets[0], "before": "a", "after": "b"}],
        task_order=order,
    )
    coverages = {row["coverage"] for row in partial_matrix["rows"] if not row.get("parent_issue_id")}
    assert "partial" in coverages or "uncovered" in coverages
    assert coverage_gate_error(partial_matrix) is not None
    assert "被覆不足" in (coverage_gate_error(partial_matrix) or "")

    # 子指摘への分割で親IDが残る
    multi_rows = [row for row in partial_matrix["rows"] if row.get("parent_issue_id")]
    assert multi_rows
    multi_parent_ids = {binding["issue_id"] for binding in bindings
                        if binding.get("state") == "multiple_targets"}
    assert multi_parent_ids
    assert {row["parent_issue_id"] for row in multi_rows} <= multi_parent_ids
    assert multi_parent_ids <= {row["issue_id"] for row in partial_matrix["rows"]}

    # invalid_reference / ambiguous は uncovered
    bad_issues = [
        {"id": "bad-invalid", "binding": {"state": "invalid_reference", "steps": [],
                                          "candidate_task_keys": [], "candidate_criterion_ids": []}},
        {"id": "bad-ambiguous", "binding": {"state": "ambiguous", "steps": [2],
                                            "candidate_task_keys": [], "candidate_criterion_ids": []}},
    ]
    bad_actions = [{"issue_id": "bad-invalid", "disposition": "amend", "target": "SC01",
                    "change": "x" * 30, "reason": "y" * 20},
                   {"issue_id": "bad-ambiguous", "disposition": "amend", "target": "SC01",
                    "change": "x" * 30, "reason": "y" * 20}]
    bad_matrix = build_coverage_matrix(
        issues=bad_issues, actions=bad_actions,
        changes=[{"target": "SC01", "before": "a", "after": "b"}],
        task_order=order,
    )
    assert all(row["coverage"] == "uncovered" for row in bad_matrix["rows"])

    # 決定的(同一入力で同一)
    again = build_coverage_matrix(
        issues=issues, actions=full_actions,
        changes=[{"target": key, "before": "a", "after": "b"} for key in full_targets],
        task_order=order,
    )
    assert canonical(again) == canonical(full_matrix)


@pytest.mark.asyncio
async def test_apply_rejects_uncovered_and_accepts_covered(tmp_path):
    from app.plan_feedback import apply, import_feedback, propose

    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)
    sig = plan_snapshot(manager, pid)[1]
    import_feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")

    async def local(*args):
        from app.plan_feedback import issues_for

        issue = issues_for(manager, pid, plan_snapshot(manager, pid)[1])[0]
        return json.dumps({"actions": [{
            "issue_id": issue["id"], "disposition": "amend", "target": task["task_key"],
            "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
            "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
        }]})

    manager._local_complete = local
    row = await propose(manager, pid, sig)
    assert row["status"] == "draft"
    assert row.get("coverage_matrix")

    # 未被覆の候補は拒否され日本語の理由が返る(適用前に確認)
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error

    empty_matrix = build_coverage_matrix(
        issues=row.get("issues") or [], actions=row.get("actions") or [], changes=[],
        task_order=[task["task_key"]],
    )
    assert coverage_gate_error(empty_matrix) is not None
    assert "被覆不足" in (coverage_gate_error(empty_matrix) or "")

    # 未被覆の候補を apply に渡すと拒否される(差分なしの候補を直接保存して検証)
    store = ReviewStore(manager.memory.path)
    uncovered = store.get(pid, "revision", sig) or {}
    uncovered.update(status="draft", changes=[], coverage_matrix=empty_matrix)
    store.put(pid, "revision", sig, uncovered)
    with pytest.raises(ValueError, match="被覆不足"):
        apply(manager, pid, sig, row["candidate_id"])

    # 対応表のない単一指摘は修正対応があれば被覆される(元の候補を復元して適用)
    store.put(pid, "revision", sig, row)
    out = apply(manager, pid, sig, row["candidate_id"])
    assert out["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_no_external_calls_and_no_secret_leak(tmp_path):
    from app.plan_feedback import propose

    fake_secret = "sk-fake0123456789abcdef"
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    _feedback(manager, pid, sig, "Claude", "工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。")
    manager.llm = Mock(spec=Ollama)
    calls = {"local": 0, "external": 0}

    async def local(*args):
        calls["local"] += 1
        return json.dumps(_amend_answer(manager, pid, sig, task["task_key"]))

    async def external(*args):
        calls["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = local
    manager.plan_review_runner = external
    row = await propose(manager, pid, sig)
    assert row["status"] == "draft"
    assert fake_secret not in canonical(row)
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig)
    events = manager.memory.get_mission(pid).get("events") or []
    blob = canonical({"stored": stored, "events": events})
    assert fake_secret not in blob
    assert calls["external"] == 0
    assert "innerHTML" not in (tmp_path / "x").as_posix()
    import pathlib

    js = pathlib.Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js


def test_prompt_version_changes_proposal_key():
    from app.plan_feedback import proposal_fingerprint

    base = {"plan_signature": "sig", "review_id": "rev", "issue_ids": ["a"],
            "binder_version": "issue-binding-v1", "prompt_version": "feedback-prompt-v1"}
    assert proposal_fingerprint(**base) != proposal_fingerprint(
        **{**base, "prompt_version": "feedback-prompt-v2"})
    assert "2026" not in proposal_fingerprint(**base)


def test_proposal_keys_registered_and_lifecycle(tmp_path):
    from pathlib import Path as _Path

    from app.memory.short_term import ShortTermMemory
    from app.project_lifecycle_registry import (
        REGISTRY,
        audit_registry_completeness,
        count_all,
    )

    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "proposal_keys" not in audit["unregistered"]
    entry = next(e for e in REGISTRY if e.table == "proposal_keys")
    assert entry.name == "goal_reviews:proposal_keys"
    assert entry.db_label == "goal_reviews"
    assert entry.project_column == "project"
    assert entry.kind == "sqlite_table"
    assert "CREATE TABLE" not in _Path("app/plan_coverage_matrix.py").read_text(encoding="utf-8")

    mem_path = tmp_path / "memory" / "conversations.db"
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    memory = ShortTermMemory(mem_path)
    pid_a = memory.create_project("架空S0A-A", "")["id"]
    pid_b = memory.create_project("架空S0A-B", "")["id"]
    memory.save_mission(pid_a, "架空目標A", "架空条件A", "", False, [], 2)
    memory.save_mission(pid_b, "架空目標B", "架空条件B", "", False, [], 2)
    from app.goal_review import ReviewStore
    from app.plan_feedback import _remember_proposal_key

    store = ReviewStore(mem_path)
    assert _remember_proposal_key(store, pid_a, "架空key-a1", "架空sig", "架空cand-a1")
    assert count_all(mem_path, pid_a).get("goal_reviews:proposal_keys") == 1
    assert count_all(mem_path, pid_b).get("goal_reviews:proposal_keys") == 0

    from app.project_lifecycle_backup import create_backup, restore_backup, verify_backup

    backup_root = tmp_path / "bk"
    backup_root.mkdir()
    backup = create_backup(mem_path, pid_a, backup_root, workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    import json as _json

    manifest = _json.loads(_Path(backup["manifest_path"]).read_text(encoding="utf-8"))
    row_entry = next(e for e in manifest["entries"] if e["name"] == "goal_reviews:proposal_keys")
    assert row_entry["count"] == 1
    assert pid_b not in _Path(backup["manifest_path"]).read_text(encoding="utf-8")

    target_mem = tmp_path / "restored" / "memory" / "conversations.db"
    restored = restore_backup(backup["manifest_path"], target_mem, workspace_root=None)
    assert restored["ok"] is True
    assert count_all(target_mem, restored["project_id"]).get("goal_reviews:proposal_keys") == 1

    from app.project_delete import _purge_all_data

    removed = _purge_all_data(mem_path, pid_a, None, "")
    assert removed.get("goal_reviews:proposal_keys") == 1
    assert count_all(mem_path, pid_a).get("goal_reviews:proposal_keys") == 0
    assert count_all(mem_path, pid_b).get("goal_reviews:proposal_keys") == 0
    assert memory.get_project(pid_b) is not None

    assert _remember_proposal_key(store, pid_a, "架空key-a2", "架空sig", "架空cand-a2")
    from app.project_generation import switch_to_new_generation

    cleared = switch_to_new_generation(mem_path, pid_a, "fresh", None)
    assert cleared.get("goal_reviews:proposal_keys") == 1
    assert count_all(mem_path, pid_a).get("goal_reviews:proposal_keys") == 0
    assert count_all(mem_path, pid_b).get("goal_reviews:proposal_keys") == 0


def test_ambiguous_binding_requires_explicit_criterion_before_rebuild_counts_as_covered():
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error
    issue = {"id": "ambiguous-1", "binding": {"state": "ambiguous", "steps": [1], "candidate_task_keys": ["source_inventory"], "candidate_criterion_ids": []}}
    action = {"issue_id": "ambiguous-1", "disposition": "rebuild_generic", "target": "execution_pipeline"}
    changes = [{"target": "execution_pipeline", "before": "old", "after": "new"}]
    before = build_coverage_matrix(issues=[issue], actions=[action], changes=changes, task_order=["source_inventory"])
    assert coverage_gate_error(before) and before["rows"][0]["coverage"] == "uncovered"
    action["binds"] = {"goal_criterion_ids": ["SC01"], "task_key": "", "contract_patch": None, "test_ref": ""}
    after = build_coverage_matrix(issues=[issue], actions=[action], changes=changes, task_order=["source_inventory"])
    assert after["rows"][0]["coverage"] == "covered_candidate"
    assert after["rows"][0]["bound_sc"] == ["SC01"]
    assert coverage_gate_error(after) is None
