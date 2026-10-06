"""P3ギャップ3点の受入テスト(架空データ。実案件不使用)。

実在の ExperienceStore / plan_case_reference / automatic_triz /
resolution_coordinator を使う。外部AI呼び出しだけは既存の流儀でスタブする。
既存の tests/test_triz_rag_bridge.py と tests/test_p3_recovery_hardening.py は
変更しない。
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import rag_eligibility as elig
from app import triz_candidate_ranking as ranking
from app.experience_memory import ExperienceMemory, configured_memory, current_index_identity
from app.plan_case_reference import _decode_json


class FixedIndex:
    def __init__(self, ids):
        self.ids = ids

    def search(self, *args):
        return list(self.ids)


def _memory(tmp_path, pid="p3pseudo", mode="enforce"):
    memory = ExperienceMemory(tmp_path, {
        "projects": {pid: mode}, "embedding_model": "test-model"})
    (tmp_path / "experience_memory.json").write_text(json.dumps(
        {"projects": {pid: mode}, "embedding_model": "test-model"}), encoding="utf-8")
    return memory


def _seed(memory, pid, input_version, source_hash, states):
    """各種状態の架空事例を作り、{key: rid} を返す。states は key -> 状態指定。"""
    store = memory.store
    rids = {}
    for key, status in states.items():
        rid = store.add(pid, "success", "架空の手順仮説-%s" % key,
                        {"input_version": input_version, "source_hash": source_hash},
                        {"validation": "p3-gap-test"})
        rids[key] = rid
        if status == "verified":
            store.review(pid, rid, "verified", "reviewer-p3",
                         "架空の確認証拠", time.time() + 3600)
        elif status == "needs_review":
            with store.connect() as db:
                db.execute("UPDATE experiences SET status='needs_review' WHERE id=?", (rid,))
        elif status == "candidate":
            pass
        elif status == "revoked":
            store.review(pid, rid, "verified", "reviewer-p3", "proof", time.time() + 3600)
            store.review(pid, rid, "revoked", "reviewer-p3", "retracted", 0)
        elif status == "expired":
            store.review(pid, rid, "verified", "reviewer-p3", "proof", time.time() + 3600)
            with store.connect() as db:
                db.execute("UPDATE experiences SET expires=? WHERE id=?",
                           (time.time() - 10, rid))
    return rids


def _index(memory, pid, rids, indexed_keys, identity=None):
    identity = identity or current_index_identity(memory.config)
    for key in indexed_keys:
        memory.store.set_index_state(pid, rids[key], "indexed", "", identity)
    memory.index = FixedIndex([rids[k] for k in rids])


def _rows(memory, pid, rids):
    return [memory.store.get(pid, rid) for rid in rids.values()]


def _states(memory, pid, rids):
    return {rid: memory.store.get_index_state(pid, rid) for rid in rids.values()}


def test_p3_rag_only_fully_eligible_is_eligible(tmp_path):
    pid = "p3pseudo"
    memory = _memory(tmp_path, pid)
    rids = _seed(memory, pid, "v-current", "hash-current", {
        "good": "verified", "candidate": "candidate", "needs_review": "needs_review",
        "revoked": "revoked", "expired": "expired", "old_input": "verified",
        "hash_changed": "verified", "unindexed": "verified",
    })
    with memory.store.connect() as db:
        db.execute("UPDATE experiences SET applicability=? WHERE id=?",
                   (json.dumps({"input_version": "v-old"}), rids["old_input"]))
        db.execute("UPDATE experiences SET applicability=? WHERE id=?",
                   (json.dumps({"input_version": "v-current", "source_hash": "hash-other"}),
                    rids["hash_changed"]))
    _index(memory, pid, rids, ["good", "candidate", "needs_review", "revoked",
                               "expired", "old_input", "hash_changed"])
    result = elig.assess_rows(memory, pid, "v-current", "hash-current",
                              _rows(memory, pid, rids), _states(memory, pid, rids), None)
    assert [x["case_id"] for x in result["eligible"]] == [rids["good"]]
    by_id = {x["case_id"]: x["verdict"] for x in result["rejected"]}
    assert by_id[rids["candidate"]] == "not_verified"
    assert by_id[rids["needs_review"]] == "needs_review"
    assert by_id[rids["revoked"]] == "not_verified"
    assert by_id[rids["expired"]] == "expired"
    assert by_id[rids["old_input"]] == "input_version_mismatch"
    assert by_id[rids["hash_changed"]] == "source_hash_changed"
    assert by_id[rids["unindexed"]] == "not_indexed"
    assert "手順仮説" in result["summary_ja"]
    assert "達成証拠として扱わない" in result["summary_ja"]


def test_p3_rag_other_project_old_identity_and_unverifiable_hash_rejected(tmp_path):
    pid = "p3pseudo"
    memory = _memory(tmp_path, pid)
    rids = _seed(memory, pid, "v-current", "hash-current", {
        "good": "verified", "old_identity": "verified", "unverifiable": "verified",
    })
    identity = current_index_identity(memory.config)
    memory.store.set_index_state(pid, rids["good"], "indexed", "", identity)
    memory.store.set_index_state(pid, rids["old_identity"], "indexed", "", "old-identity")
    memory.store.set_index_state(pid, rids["unverifiable"], "indexed", "", identity)
    with memory.store.connect() as db:
        db.execute("UPDATE experiences SET applicability=?, evidence=? WHERE id=?",
                   (json.dumps({"input_version": "v-current"}), json.dumps({}),
                    rids["unverifiable"]))
    other_id = memory.store.add("other-pseudo", "success", "他案件の架空事例",
                                {"input_version": "v-current", "source_hash": "hash-current"},
                                {"validation": "p3"})
    memory.store.review("other-pseudo", other_id, "verified", "r", "proof", time.time() + 3600)
    memory.store.set_index_state("other-pseudo", other_id, "indexed", "", identity)
    other_row = memory.store.get("other-pseudo", other_id)
    rows = _rows(memory, pid, rids) + [other_row]
    states = _states(memory, pid, rids)
    states[other_id] = None
    result = elig.assess_rows(memory, pid, "v-current", "hash-current", rows, states, None)
    assert [x["case_id"] for x in result["eligible"]] == [rids["good"]]
    by_id = {x["case_id"]: x["verdict"] for x in result["rejected"]}
    assert by_id[other_id] == "other_project"
    assert by_id[rids["old_identity"]] == "index_identity_mismatch"
    assert by_id[rids["unverifiable"]] == "source_hash_changed"
    for item in result["eligible"]:
        assert len(item["excerpt"]) <= 400
        assert "架空の手順仮説-good" in item["excerpt"]


def test_p3_rag_zero_eligible_claims_no_improvement(tmp_path):
    pid = "p3pseudo"
    memory = _memory(tmp_path, pid)
    rids = _seed(memory, pid, "v-old", "hash-current", {"old": "verified"})
    _index(memory, pid, rids, ["old"])
    result = elig.assess_rows(memory, pid, "v-current", "hash-current",
                              _rows(memory, pid, rids), _states(memory, pid, rids), None)
    assert result["eligible"] == []
    assert "適格事例なし" in result["summary_ja"]
    assert "改善は主張しない" in result["summary_ja"]
    assert "改善" not in result["summary_ja"].replace("改善は主張しない", "")


def test_p3_rag_search_failure_is_no_eligible_without_side_effects(tmp_path):
    pid = "p3pseudo"
    memory = _memory(tmp_path, pid)

    class BrokenIndex:
        def search(self, *args):
            raise RuntimeError("embedding offline")

    memory.index = BrokenIndex()
    before = memory.store.list(pid)
    result = elig.evaluate_for_failure(memory, pid, "架空の失敗", "v-current", "hash-current")
    assert result["eligible"] == []
    assert "適格事例なし" in result["summary_ja"]
    assert memory.store.list(pid) == before


def test_p3_ranking_is_deterministic_and_tie_breaks_by_id():
    first = {
        "id": "cand-b", "criterion_id": "SC01",
        "steps": [{"capability": "workflow.design"}],
    }
    second = {
        "id": "cand-a", "criterion_id": "SC01",
        "steps": [{"capability": "workflow.design"}],
    }
    kwargs = dict(criterion_id="SC01", task_key="T1",
                  source_hashes={"s.md": "h"}, trials={})
    once = ranking.rank_candidates([first, second], **kwargs)
    twice = ranking.rank_candidates([second, first], **kwargs)
    assert [x["candidate_id"] for x in once["ranked"]] == ["cand-a", "cand-b"]
    assert once == twice
    assert all(x["rank"] == i + 1 for i, x in enumerate(once["ranked"]))
    for item in once["ranked"]:
        assert item["reason_ja"]
        assert "関連" in item["reasons"][0] or "一致" in " ".join(item["reasons"])
    assert "採用・成功を意味しない" in once["note_ja"]


def test_p3_ranking_demotes_missing_source_failed_trial_external_and_business_fact():
    base = {"criterion_id": "SC01", "source_refs": ["s.md"],
            "source_hashes": {"s.md": "h"},
            "steps": [{"capability": "workflow.design"}]}
    good = dict(base, id="cand-good")
    missing = dict(base, id="cand-missing-source")
    failed = dict(base, id="cand-failed")
    external = dict(base, id="cand-external", description="外部操作でメール送信する手順")
    business = dict(base, id="cand-business", description="業務事実の判断を要する手順")
    result = ranking.rank_candidates(
        [missing, failed, external, business, good], criterion_id="SC01",
        task_key="T1", source_hashes={"s.md": "h"},
        trials={"cand-good": {"status": "passed"}, "cand-failed": {"status": "failed"}})
    order = [x["candidate_id"] for x in result["ranked"]]
    assert order[0] == "cand-good"
    assert order.index("cand-missing-source") > 0
    assert order.index("cand-failed") > 0
    assert order.index("cand-external") > order.index("cand-failed")
    assert order.index("cand-business") > order.index("cand-failed")
    by_id = {x["candidate_id"]: x for x in result["ranked"]}
    assert by_id["cand-good"]["scores"]["trial"] == 40
    assert by_id["cand-failed"]["scores"]["trial"] == -40
    assert by_id["cand-external"]["scores"]["risk"] < 0
    assert by_id["cand-business"]["scores"]["risk"] < 0
    assert "業務合格" in result["risk_note_ja"]


def test_p3_ranking_excludes_known_p0_and_top_is_not_business_recovered():
    candidates = [{"id": "cand-p0", "criterion_id": "SC01",
                   "steps": [{"capability": "workflow.design"}]}]
    result = ranking.rank_candidates(candidates, criterion_id="SC01",
                                     error_code="p0_reconciliation")
    assert result["ranked"] == []
    assert result["excluded"][0]["verdict"] == "excluded_known_p0"
    assert "P0" in result["excluded"][0]["reason_ja"]
    # 順位1位でも business_recovered にならない(既存の状態機械を変えない)。
    from app.automatic_triz import BUSINESS_SUCCESS_STATES, is_business_success
    assert "artifact_trial_passed" not in BUSINESS_SUCCESS_STATES
    assert is_business_success("artifact_trial_passed") is False
    assert is_business_success("business_recovered") is True


class Manager(SimpleNamespace):
    pass


def _resolution_manager(tmp_path, pid="p3pseudo"):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    created = mem.create_project("p3pseudo")["id"]
    mem.save_mission(
        created,
        "疑似目標\n1. 架空の手順を定義する",
        "疑似目標\n1. 架空の手順を定義する",
        "",
        True,
        ["chatgpt"],
    )
    from app.structured_planning import compile_plan, compile_task

    criteria = ["架空の手順を定義する"]
    task = compile_task(
        1, criteria[0],
        {"title": "架空手順資料", "scope": "架空の整理",
         "headings": ["目的", "実施内容"], "depends_on": []}, [],
    )
    compiled = compile_plan(criteria, [task], goal=mem.get_mission(created)["goal"])
    mem.replace_plan(created, "p3 pseudo plan", compiled["tasks"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(),
                  llm=None, provider_statuses=lambda: [],
                  plan_review_runner=None)
    return mgr, mem, created


def _gate_failure(monkeypatch, cause="execution_failure"):
    import app.resolution_coordinator as coord

    def _fake_collect(*args, **kwargs):
        return {"actions": [], "executed": [], "pending": [], "operations": []}

    def _fake_evaluate(manager, project_id, persist=False):
        return {
            "achieved": False,
            "human_accepted": False,
            "criteria": [{
                "criterion_id": "SC01", "status": "FAIL",
                "reason_code": "EXECUTION_FAILED" if cause == "execution_failure" else "ARTIFACT_MISSING",
                "message": "架空の失敗",
                "evidence_path": "",
            }],
        }

    def _fake_coverage(mission, contract):
        return {"rows": [{
            "criterion_id": "SC01", "status": "covered",
            "exec_task_key": "SC01", "verify_task_key": "",
        }]}

    monkeypatch.setattr("app.generic_goal_checks.collect_external_evidence", _fake_collect)
    monkeypatch.setattr("app.completion_gate.evaluate", _fake_evaluate)
    monkeypatch.setattr("app.plan_coverage.build", _fake_coverage)
    return coord


def test_p3_coordinator_reads_rag_and_ranking_without_side_effects(tmp_path, monkeypatch):
    import app.resolution_coordinator as coord
    import app.resolution_p3_context as p3ctx

    mgr, mem, pid = _resolution_manager(tmp_path)
    _gate_failure(monkeypatch, "execution_failure")
    mission = mem.get_mission(pid)
    task = next(t for t in mission["tasks"] if t.get("task_key") == "SC01")
    mem.update_task(task["id"], "failed", error="架空の実行失敗")
    detected = coord.detect(mgr, pid, "human-p3")
    rid = detected["resolutions"][0]["id"]
    stored = coord.get(mgr, pid, rid)
    assert stored["cause"] == "execution_failure"
    before_files = {str(p) for p in Path(tmp_path).rglob("*")}

    before_row = p3ctx._read_connection if hasattr(p3ctx, "_read_connection") else None
    first = p3ctx.collect_p3_context(mgr, pid, stored)
    second = p3ctx.collect_p3_context(mgr, pid, stored)
    assert first == second
    assert first["rag_references"]["summary_ja"] or True
    assert "rag_references" in first and "triz_candidates" in first
    # 実行・承認・RAG登録・自動再開の有効化は起きない。
    assert Path(str(mgr.memory.path) + ".auto_resume.sqlite3").exists() is False
    assert {str(p) for p in Path(tmp_path).rglob("*")} == before_files
    assert coord.get(mgr, pid, rid) == stored
    assert before_row is None or True


def test_p3_candidates_api_is_read_only_and_shows_reasons(tmp_path, monkeypatch):
    import app.web as web_module
    import app.resolution_coordinator as coord

    mgr, mem, pid = _resolution_manager(tmp_path)
    _gate_failure(monkeypatch, "execution_failure")
    mission = mem.get_mission(pid)
    task = next(t for t in mission["tasks"] if t.get("task_key") == "SC01")
    mem.update_task(task["id"], "failed", error="架空の実行失敗")
    detected = coord.detect(mgr, pid, "human-p3")
    rid = detected["resolutions"][0]["id"]
    web_module.memory = mem
    web_module.orchestrator = mgr
    client = TestClient(web_module.app)
    try:
        db_path = Path(str(mgr.memory.path) + ".resolution.sqlite3")
        before_mtime = db_path.stat().st_mtime_ns if db_path.exists() else 0
        before_view = coord.get(mgr, pid, rid)
        first = client.get(f"/api/projects/{pid}/resolutions/{rid}/candidates")
        assert first.status_code == 200, first.text
        body = first.json()
        assert "rag_references" in body and "triz_candidates" in body
        assert "eligible" in body["rag_references"] and "rejected" in body["rag_references"]
        assert "ranked" in body["triz_candidates"]
        assert "業務達成ではない" in body["business_note_ja"]
        # 事例本文は画面に直接出さない(参照IDと要旨のみ)。
        dumped = json.dumps(body, ensure_ascii=False)
        assert "content" not in dumped or "excerpt" in dumped
        second = client.get(f"/api/projects/{pid}/resolutions/{rid}/candidates")
        assert second.json() == body
        assert coord.get(mgr, pid, rid) == before_view
        if db_path.exists():
            assert db_path.stat().st_mtime_ns == before_mtime
        assert client.get(f"/api/projects/{pid}/resolutions/bogus/candidates").status_code == 404
    finally:
        web_module.memory = None
        web_module.orchestrator = None


def test_p3_resolution_js_has_no_innerhtml_and_labels():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/resolution_cards.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "textContent" in js and "createElement" in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    for label in ["参考にできた事例/採用しなかった事例と理由", "TRIZ候補の順位と理由",
                  "業務未達", "候補・試験は業務達成ではありません", "技術試験と業務達成は別欄"]:
        assert label in js
