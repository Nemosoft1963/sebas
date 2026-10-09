"""Stage 0-5: generality acceptance (0-1/0-2/0-3 across domains and scales).

疑似PJのみ(架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
"""
import asyncio
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import Ollama
from app.experience_store import canonical
from app.goal_review import ReviewStore, plan_snapshot, review_budget
from app.memory.short_term import ShortTermMemory
from app.plan_feedback import apply, import_feedback, issues_for, propose
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task, contract_of
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]

# 工程数×指摘数の代表組合せ。60秒以内に収めるため代表点に絞る。
E2E_CASES = [
    (3, 1), (3, 3), (8, 3), (8, 6), (20, 6), (20, 12), (40, 6),
]

STEP_RAWS = ["3", "1,4", "2-5", "1,3-4,8", "999", "5-2", "1,,2", "abc", ""]

SAFE_CHANGE = "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。"
SAFE_REASON = "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。"


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
    assert "架空" in f"架空-{domain}"  # 題材は架空
    name = f"s0c-疑似-{domain}-{n}{tag}"
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


def _import_issues(manager, pid, sig, domain, k, start=0):
    for pos in range(k):
        idx = start + pos
        text = (f"架空指摘{idx}: 原本と結果の照合が不足しています。"
                f"原本の参照箇所と照合結果を対応付けてください。(域={domain} 連番={idx})")
        import_feedback(manager, pid, sig, f"架空提供者{idx}", text)


def _amendable_keys(manager, pid):
    snapshot = plan_snapshot(manager, pid)[0]
    keys = []
    for task in snapshot["tasks"]:
        if not (contract_of(task) or {}).get("execution_kind"):
            keys.append(task["task_key"])
    return keys


def _install_stubs(manager, counter):
    manager.llm = Mock(spec=Ollama)

    async def local(*args):
        counter["local"] += 1
        sig = plan_snapshot(manager, manager._s0c_pid)[1]
        current = issues_for(manager, manager._s0c_pid, sig)
        amendable = _amendable_keys(manager, manager._s0c_pid)
        assert amendable, "amend可能な工程が必要"
        data = {"actions": []}
        for pos, issue in enumerate(current):
            target = amendable[pos % len(amendable)]
            data["actions"].append({
                "issue_id": issue["id"], "disposition": "amend", "target": target,
                "change": SAFE_CHANGE, "reason": SAFE_REASON,
            })
        return json.dumps(data)

    async def external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = local
    manager.plan_review_runner = external


def _snapshot_tasks_for_binding(n):
    import json as _json
    tasks = []
    for i in range(1, n + 1):
        key = f"SC{i:02d}"
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "架空疑似工程",
            "acceptance_criteria": _json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": [key],
            }, ensure_ascii=False),
            "depends_on": [],
        })
    return tasks


def _contract_for_binding(n):
    return {"criteria": [
        {"criterion_id": f"SC{i:02d}", "exec_task_keys": [f"SC{i:02d}"],
         "verify_task_keys": [f"SC{i:02d}"]} for i in range(1, n + 1)]}


# --- 書式の一般性: parse_step_reference は規模を変えても決定的 ---

@pytest.mark.parametrize("n", [3, 8, 20, 40])
@pytest.mark.parametrize("raw", STEP_RAWS)
def test_parse_step_reference_scales_and_formats(n, raw):
    from app.plan_review_loop import parse_step_reference
    first = parse_step_reference(raw, n)
    second = parse_step_reference(raw, n)
    assert first == second  # 決定的
    if first["ok"]:
        assert first["steps"] == sorted(first["steps"])
        assert all(1 <= s <= n for s in first["steps"])
        assert len(first["steps"]) <= 16
    else:
        assert first["steps"] == []
        assert first["reason"]


@pytest.mark.parametrize("n", [3, 8, 20, 40])
def test_binding_states_cover_single_multi_invalid(n):
    from app.plan_review_loop import build_issue_bindings
    snapshot = {"tasks": _snapshot_tasks_for_binding(n)}
    contract = _contract_for_binding(n)

    def _one(step_raw, extra=None):
        issue = {"severity": "blocking", "step": step_raw,
                 "unmet_goal": "架空未達", "reason": "架空理由の追記",
                 "remedy": "架空対応を追記する"}
        if extra:
            issue.update(extra)
        return issue

    single_n = min(3, n)
    table = build_issue_bindings(
        [{"provider": "架空外部", "status": "fail",
          "issues": [_one(str(single_n))]}], contract, snapshot)
    assert table["bindings"][0]["state"] == "resolved"
    assert len(table["bindings"][0]["candidate_task_keys"]) == 1

    if n >= 4:
        table2 = build_issue_bindings(
            [{"provider": "架空外部", "status": "fail",
              "issues": [_one("1,4")]}], contract, snapshot)
        row = table2["bindings"][0]
        assert row["state"] == "multiple_targets"
        assert len(row["candidate_task_keys"]) > 1  # 縮約しない

    bad = build_issue_bindings(
        [{"provider": "架空外部", "status": "fail", "issues": [_one("9999")]}],
        contract, snapshot)
    assert bad["bindings"][0]["state"] == "invalid_reference"

    rev = build_issue_bindings(
        [{"provider": "架空外部", "status": "fail", "issues": [_one("5-2")]}],
        contract, snapshot)
    assert rev["bindings"][0]["state"] == "invalid_reference"

    amb = build_issue_bindings(
        [{"provider": "架空外部", "status": "fail",
          "issues": [_one("1", {"reason": "架空SC99の検証不足"})]}],
        contract, snapshot)
    assert amb["bindings"][0]["state"] == "ambiguous"


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("n", [8, 20])
def test_coverage_single_change_cannot_cover_all(domain, n):
    from app.plan_coverage_matrix import build_coverage_matrix
    order = [f"SC{i:02d}" for i in range(1, n + 1)]
    issues = [
        {"id": f"架空-{domain}-a", "binding": {"state": "resolved", "steps": [1],
         "candidate_task_keys": [order[0]], "candidate_criterion_ids": [order[0]]}},
        {"id": f"架空-{domain}-b", "binding": {"state": "resolved", "steps": [2],
         "candidate_task_keys": [order[1]], "candidate_criterion_ids": [order[1]]}},
        {"id": f"架空-{domain}-c", "binding": {"state": "resolved", "steps": [3],
         "candidate_task_keys": [order[2]], "candidate_criterion_ids": [order[2]]}},
    ]
    actions = [{"issue_id": it["id"], "disposition": "amend", "target": it["binding"]["candidate_task_keys"][0],
                "change": "x" * 30, "reason": "y" * 20} for it in issues]
    matrix = build_coverage_matrix(
        issues=issues, actions=actions,
        changes=[{"target": order[0], "before": "a", "after": "b"}],
        task_order=order)
    covered = [r for r in matrix["rows"] if r["coverage"] == "covered_candidate"]
    # 入力から導く: 1件の変更では3件全ては被覆できない
    assert len(covered) < len(issues)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("n", [8, 20])
def test_coverage_full_partial_invalid_deterministic(domain, n):
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error
    order = [f"SC{i:02d}" for i in range(1, n + 1)]
    issues = [
        {"id": f"架空-{domain}-1", "binding": {"state": "resolved", "steps": [1],
         "candidate_task_keys": [order[0]], "candidate_criterion_ids": [order[0]]}},
        {"id": f"架空-{domain}-2", "binding": {"state": "resolved", "steps": [2],
         "candidate_task_keys": [order[1]], "candidate_criterion_ids": [order[1]]}},
    ]
    actions = [{"issue_id": it["id"], "disposition": "amend", "target": it["binding"]["candidate_task_keys"][0],
                "change": "x" * 30, "reason": "y" * 20} for it in issues]
    full = build_coverage_matrix(
        issues=issues, actions=actions,
        changes=[{"target": order[0], "before": "a", "after": "b"},
                 {"target": order[1], "before": "a", "after": "b"}],
        task_order=order)
    assert all(r["coverage"] == "covered_candidate" for r in full["rows"])
    assert coverage_gate_error(full) is None

    partial = build_coverage_matrix(
        issues=issues, actions=actions,
        changes=[{"target": order[0], "before": "a", "after": "b"}],
        task_order=order)
    assert coverage_gate_error(partial) is not None

    bad = build_coverage_matrix(
        issues=[{"id": f"架空-{domain}-bad", "binding": {"state": "invalid_reference",
                "steps": [], "candidate_task_keys": [], "candidate_criterion_ids": []}}],
        actions=[{"issue_id": f"架空-{domain}-bad", "disposition": "amend",
                  "target": order[0], "change": "x" * 30, "reason": "y" * 20}],
        changes=[{"target": order[0], "before": "a", "after": "b"}],
        task_order=order)
    assert bad["rows"][0]["coverage"] == "uncovered"

    again = build_coverage_matrix(
        issues=issues, actions=actions,
        changes=[{"target": order[0], "before": "a", "after": "b"},
                 {"target": order[1], "before": "a", "after": "b"}],
        task_order=order)
    assert canonical(again) == canonical(full)


# --- 冪等・適用・外部0の一般性: 分野×規模×指摘数 ---

@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("n,k", E2E_CASES)
async def test_e2e_idempotent_apply_no_external(tmp_path, domain, n, k):
    manager, pid = _make_manager(tmp_path, domain, n, tag=f"-{k}")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    version_before = manager.memory.get_mission(pid)["plan_version"]
    budget_before = review_budget(manager, pid)
    rounds_before = len((ReviewStore(manager.memory.path).get(pid, "plan", sig) or {}).get("reviews") or [])
    _import_issues(manager, pid, sig, domain, k)

    first = await propose(manager, pid, sig)
    assert first["status"] == "draft"
    assert len(first["issues"]) == k  # 期待値は入力の指摘数から導く
    assert counter["local"] == 1
    # 逐次再送は冪等
    second = await propose(manager, pid, sig)
    assert second["candidate_id"] == first["candidate_id"]
    assert counter["local"] == 1
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig)
    assert stored["attempts"] == 1

    # 入力が変わると新候補(重ならない連番を追加する)
    _import_issues(manager, pid, sig, domain, 1, start=k)
    third = await propose(manager, pid, sig)
    assert third["candidate_id"] != first["candidate_id"]
    assert counter["local"] == 2

    out = apply(manager, pid, sig, third["candidate_id"])
    assert out["signature"] != sig
    assert out["external_sends"] == 0
    assert out["local_reevaluation"]["passed"] is True
    assert out["local_reevaluation"]["external_sends"] == 0
    assert out["local_reevaluation"]["external_calls"] == 0
    # 実測の記録(固定値でない): ラウンド増分0・予算維持・上限2
    assert out["external_measurement"]["external_rounds_delta"] == 0
    assert out["external_measurement"]["external_budget_kept"] is True
    assert out["external_measurement"]["external_limit"] == 2
    assert manager.memory.get_mission(pid)["plan_version"] == version_before + 1
    budget_after = review_budget(manager, pid)
    assert budget_after == budget_before
    store = ReviewStore(manager.memory.path)
    rounds_after = len((store.get(pid, "plan", out["signature"]) or {}).get("reviews") or [])
    assert rounds_after == 0
    assert rounds_before == 0
    assert counter["external"] == 0
    # 二重適用は版を増やさない
    with pytest.raises(ValueError, match="反映対象の修正案がありません"):
        apply(manager, pid, sig, third["candidate_id"])
    assert manager.memory.get_mission(pid)["plan_version"] == version_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
async def test_concurrent_propose_single_candidate(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 8, tag="-conc")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    _import_issues(manager, pid, sig, domain, 3)
    rows = await asyncio.gather(*[propose(manager, pid, sig) for _ in range(8)])
    assert len({r["candidate_id"] for r in rows}) == 1
    assert counter["local"] == 1
    assert counter["external"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
async def test_concurrent_apply_single_version_bump(tmp_path, domain):
    manager, pid = _make_manager(tmp_path, domain, 8, tag="-capp")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    _import_issues(manager, pid, sig, domain, 3)
    row = await propose(manager, pid, sig)
    before = manager.memory.get_mission(pid)["plan_version"]
    results, errors = [], []

    async def _one():
        try:
            results.append(await asyncio.to_thread(apply, manager, pid, sig, row["candidate_id"]))
        except ValueError as exc:
            errors.append(str(exc))

    await asyncio.gather(_one(), _one())
    assert len(results) == 1
    assert len(errors) == 1
    assert manager.memory.get_mission(pid)["plan_version"] == before + 1
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_claim_db_failure_is_fail_closed(tmp_path):
    from app.plan_feedback import _claim_proposal_key
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-claim")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    _import_issues(manager, pid, sig, "doc", 1)

    class _Broken:
        def connect(self):
            raise sqlite3.OperationalError("架空DB障害")

    with pytest.raises(ValueError, match="再試行"):
        _claim_proposal_key(_Broken(), pid, "架空key", sig)

    # propose経路: 生成権の確保失敗は候補を作らずLLMを呼ばない
    import app.plan_feedback as fb
    real_claim = fb._claim_proposal_key

    def _failing(store, _pid, _key, _sig):
        raise ValueError("修正案の生成権を確保できませんでした(DB障害)。再試行してください")

    fb._claim_proposal_key = _failing
    try:
        with pytest.raises(ValueError, match="生成権"):
            await propose(manager, pid, sig)
    finally:
        fb._claim_proposal_key = real_claim
    assert counter["local"] == 0
    assert counter["external"] == 0
    assert (ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}).get("status") != "draft"


@pytest.mark.asyncio
async def test_apply_rejects_uncovered_and_rolls_back(tmp_path):
    from app.plan_coverage_matrix import build_coverage_matrix
    manager, pid = _make_manager(tmp_path, "table", 8, tag="-uncov")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    before = manager.memory.get_mission(pid)["plan_version"]
    _import_issues(manager, pid, sig, "table", 3)
    row = await propose(manager, pid, sig)
    store = ReviewStore(manager.memory.path)
    empty = build_coverage_matrix(
        issues=row.get("issues") or [], actions=row.get("actions") or [], changes=[],
        task_order=_amendable_keys(manager, pid))
    broken = store.get(pid, "revision", sig) or {}
    broken.update(status="draft", changes=[], coverage_matrix=empty)
    store.put(pid, "revision", sig, broken)
    with pytest.raises(ValueError, match="被覆不足"):
        apply(manager, pid, sig, row["candidate_id"])
    assert manager.memory.get_mission(pid)["plan_version"] == before
    assert counter["external"] == 0


@pytest.mark.asyncio
async def test_apply_signature_invariant_and_reeval_failure_roll_back(tmp_path):
    import app.plan_feedback as fb
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-siginv")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    before = manager.memory.get_mission(pid)["plan_version"]
    _import_issues(manager, pid, sig, "doc", 1)
    row = await propose(manager, pid, sig)
    store = ReviewStore(manager.memory.path)

    orig = fb._apply_mutate

    def _no_change(*a, **k):
        return {"status": "awaiting_review", "signature": sig, "task_id": None,
                "public_draft": "", "lifecycle": "revalidation_pending",
                "resume_from": "public_packet"}

    fb._apply_mutate = _no_change
    try:
        with pytest.raises(ValueError, match="署名が変わりませんでした"):
            apply(manager, pid, sig, row["candidate_id"])
    finally:
        fb._apply_mutate = orig
    assert manager.memory.get_mission(pid)["plan_version"] == before
    assert store.get(pid, "revision", sig)["status"] == "failed"

    # 再評価不合格もロールバックし未解決を残す
    manager2, pid2 = _make_manager(tmp_path, "doc", 3, tag="-reeval")
    manager2._s0c_pid = pid2
    counter2 = {"local": 0, "external": 0}
    _install_stubs(manager2, counter2)
    sig2 = plan_snapshot(manager2, pid2)[1]
    before2 = manager2.memory.get_mission(pid2)["plan_version"]
    _import_issues(manager2, pid2, sig2, "doc", 1)
    row2 = await propose(manager2, pid2, sig2)
    orig_reeval = fb.local_reevaluate_applied_plan

    def _failing(*a, **k):
        out = orig_reeval(*a, **k)
        bad = dict(out)
        bad["passed"] = False
        bad["remaining"] = ["criteria_retention:missing_SC01"]
        return bad

    fb.local_reevaluate_applied_plan = _failing
    try:
        with pytest.raises(ValueError, match="ローカル再評価"):
            apply(manager2, pid2, sig2, row2["candidate_id"])
    finally:
        fb.local_reevaluate_applied_plan = orig_reeval
    assert manager2.memory.get_mission(pid2)["plan_version"] == before2
    failed = ReviewStore(manager2.memory.path).get(pid2, "revision", sig2)
    assert failed["status"] == "failed"
    carried = [i for i in (ReviewStore(manager2.memory.path).get(pid2, "feedback", sig2) or {}).get("issues", [])
               if i.get("origin") == "local_reevaluation"]
    assert carried
    assert counter2["external"] == 0


@pytest.mark.asyncio
async def test_apply_detects_external_budget_increase(tmp_path):
    import app.plan_feedback as fb
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-budget")
    manager._s0c_pid = pid
    counter = {"local": 0, "external": 0}
    _install_stubs(manager, counter)
    sig = plan_snapshot(manager, pid)[1]
    before = manager.memory.get_mission(pid)["plan_version"]
    _import_issues(manager, pid, sig, "doc", 1)
    row = await propose(manager, pid, sig)
    real_budget = fb.review_budget
    calls = {"n": 0}

    def _growing(m, p):
        calls["n"] += 1
        base = real_budget(m, p)
        if calls["n"] >= 2:
            grown = dict(base)
            grown["managed"] = True
            grown["used_calls"] = int(base.get("used_calls") or 0) + 1
            grown["remaining_calls"] = int(base.get("remaining_calls") or 0)
            return grown
        return base

    fb.review_budget = _growing
    try:
        with pytest.raises(ValueError, match="外部評価の使用量"):
            apply(manager, pid, sig, row["candidate_id"])
    finally:
        fb.review_budget = real_budget
    assert manager.memory.get_mission(pid)["plan_version"] == before
    assert counter["external"] == 0


def test_expectations_derived_from_inputs_not_hardcoded(tmp_path):
    for domain in DOMAINS:
        for n in (3, 8):
            manager, pid = _make_manager(tmp_path, domain, n, tag=f"-exp{n}")
            snapshot = plan_snapshot(manager, pid)[0]
            assert len(snapshot["tasks"]) == n
            titles = [t["title"] for t in snapshot["tasks"]]
            assert len(set(titles)) == n
    # 分野ごとに工程名・成果物が異なる
    titles_by_domain = {}
    for domain in DOMAINS:
        manager, pid = _make_manager(tmp_path, domain, 3, tag="-diff")
        titles_by_domain[domain] = sorted(t["title"] for t in plan_snapshot(manager, pid)[0]["tasks"])
    assert titles_by_domain["doc"] != titles_by_domain["table"]
    assert titles_by_domain["doc"] != titles_by_domain["external"]
    assert titles_by_domain["table"] != titles_by_domain["external"]


def test_registry_completeness_s0c():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1
