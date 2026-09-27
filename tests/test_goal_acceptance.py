import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.completion_gate import accept, evaluate
from app.goal_completion_store import GoalCompletionStore
from app.goal_contract import content_hash, from_mission


HASHES = {
    "contract_hash": "contract-1",
    "plan_signature": "plan-1",
    "input_hash": "input-1",
    "source_hash": "source-1",
    "artifact_hash": "artifact-1",
}


class Memory:
    def __init__(self, path, mission=None):
        self.path = path
        self.mission = mission or {"tasks": []}

    def get_mission(self, project_id):
        return self.mission


@pytest.fixture
def manager(tmp_path):
    return SimpleNamespace(memory=Memory(tmp_path / "memory.sqlite3"))


def _contract():
    return {
        "content_hash": HASHES["contract_hash"],
        "criteria": [{"criterion_id": "C01"}],
        "retention": {"passed": True},
        "completion_policy": {"require_human_acceptance": True},
    }


def _positive_rows(failures=None):
    rows = []
    for index in range(1, 13):
        cid = f"C{index:02d}"
        if failures:
            rows.append({
                "criterion_id": cid, "status": "FAIL", "reason_code": "CHECK_MISSING",
                "evidence_path": "vehicle_profit/input.json", "message": failures[0],
                "check_id": "check_test", "evidence_summary": "forced failure",
            })
        else:
            rows.append({
                "criterion_id": cid, "status": "PASS", "reason_code": "",
                "evidence_path": "vehicle_profit/input.json", "message": "",
                "check_id": f"check_{cid.lower()}", "evidence_summary": "ok",
            })
    return rows


def _gate_patches(hashes=None, failures=None, artifact=True):
    current = dict(HASHES if hashes is None else hashes)
    if not artifact:
        current["artifact_hash"] = ""
    return (
        patch("app.completion_gate.get_active", return_value=_contract()),
        patch("app.completion_gate.build_coverage", return_value={"passed": True, "rows": []}),
        patch("app.vehicle_workflow.applicable", return_value=True),
        patch("app.goal_checks.evaluate_vehicle_criteria", return_value=_positive_rows(failures)),
        patch("app.vehicle_workflow.goal_failures", return_value=[] if failures is None else failures),
        patch("app.vehicle_workflow.load_input", return_value={"data": {}}),
        patch("app.vehicle_workflow.source_reconciliation_report", return_value=([], {"passed": True}, [])),
        patch("app.vehicle_workflow.sources", return_value=[]),
        patch("app.completion_gate._current_hashes", return_value=current),
    )


def _evaluate(manager, hashes=None, failures=None, artifact=True):
    patches = _gate_patches(hashes, failures, artifact)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        return evaluate(manager, "p1")


def _accept(manager, hashes=None, accepted_by="Alice", artifact=True):
    patches = _gate_patches(hashes, artifact=artifact)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        return accept(manager, "p1", accepted_by=accepted_by, note="checked")


def _rows(store):
    with store.connect() as db:
        return db.execute("SELECT * FROM human_acceptances ORDER BY id").fetchall()


def test_gt02_current_version_acceptance_achieves_and_is_ledgered(manager):
    result = _accept(manager)
    assert result["achieved"] is True
    assert result["human_acceptance"]["valid"] is True
    rows = _rows(GoalCompletionStore(manager.memory.path))
    assert len(rows) == 1


@pytest.mark.parametrize(
    "changed_key",
    ["input_hash", "source_hash", "artifact_hash", "contract_hash", "plan_signature"],
)
def test_gt04_each_hash_drift_invalidates_acceptance(manager, changed_key):
    _accept(manager)
    changed = dict(HASHES)
    changed[changed_key] += "-changed"
    result = _evaluate(manager, changed)
    assert result["achieved"] is False
    assert result["human_acceptance"]["valid"] is False
    assert result["human_acceptance"]["reason"] == f"HUMAN_ACCEPTANCE_HASH_DRIFT:{changed_key}"


def test_missing_acceptance_provisional_and_completed_never_achieve(manager):
    result = _evaluate(manager)
    assert result["achieved"] is False
    assert result["reason_code"] == "HUMAN_ACCEPTANCE_MISSING"
    assert result["human_acceptance"]["reason"] == "HUMAN_ACCEPTANCE_MISSING"
    manager.memory.mission["status"] = "completed"
    completed = _evaluate(manager)
    assert completed["achieved"] is False
    assert completed["artifact_class"] == "provisional"


def test_blank_accepted_by_rejected_by_function_and_api_without_ledger_row(manager, monkeypatch):
    store = GoalCompletionStore(manager.memory.path)
    with pytest.raises(ValueError, match="accepted_by is required"):
        accept(manager, "p1", accepted_by="   ")
    assert _rows(store) == []

    import app.web as web
    monkeypatch.setattr(web, "memory", SimpleNamespace(get_project=lambda project_id: {"id": project_id}))
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app)
    response = client.post("/api/projects/p1/completion-gate/accept", json={"accepted_by": "   ", "note": ""})
    assert response.status_code == 422
    assert _rows(store) == []


def test_accept_rejects_other_failed_condition_without_ledger_row(manager):
    patches = _gate_patches(failures=["必須の計算検証がありません"])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        with pytest.raises(ValueError, match="COMPLETION_GATE_FAILED"):
            accept(manager, "p1", accepted_by="Alice")
    assert _rows(GoalCompletionStore(manager.memory.path)) == []


def test_vehicle_contract_versions_affect_hash_and_generic_hash_is_legacy_compatible(manager):
    vehicle_mission = {
        "goal": "車両別 月次 損益",
        "success_criteria": "車両別損益を確認する",
        "constraints_text": "",
        "plan_version": 1,
        "tasks": [],
    }
    versions_a = {"source_hash": "s1", "input_hash": "i1", "ocr_adopted_versions": [{"run_id": "r1", "manifest_hash": "m1"}]}
    versions_b = {**versions_a, "source_hash": "s2"}
    with patch("app.vehicle_workflow.applicable", return_value=True), patch(
        "app.vehicle_workflow.requested_months", return_value=[]
    ), patch("app.goal_contract._vehicle_source_versions", return_value=versions_a):
        first = from_mission(vehicle_mission, manager, "p1")
    with patch("app.vehicle_workflow.applicable", return_value=True), patch(
        "app.vehicle_workflow.requested_months", return_value=[]
    ), patch("app.goal_contract._vehicle_source_versions", return_value=versions_b):
        second = from_mission(vehicle_mission, manager, "p1")
    assert first["source_versions"]["source_hash"] == "s1"
    assert first["source_versions"]["input_hash"] == "i1"
    assert first["source_versions"]["ocr_adopted_versions"] == versions_a["ocr_adopted_versions"]
    assert first["content_hash"] != second["content_hash"]

    generic = {
        "goal": "一般的な報告書を作る",
        "success_criteria": "報告書を確認する",
        "constraints_text": "",
        "plan_version": 1,
        "tasks": [],
    }
    current = from_mission(generic, manager, "g1")
    legacy = dict(current)
    legacy.pop("content_hash")
    assert current["source_versions"] == {"mission_plan_version": 1, "vehicle_engine_revision": None}
    assert current["content_hash"] == content_hash(legacy)


def test_acceptance_migration_is_idempotent_and_preserves_existing_data(tmp_path):
    memory_path = tmp_path / "memory.sqlite3"
    store = GoalCompletionStore(memory_path)
    with store.connect() as db:
        db.execute(
            "INSERT INTO goal_states(project_id,state,updated_at) VALUES(?,?,?)",
            ("p1", "draft", "now"),
        )
        db.execute("""INSERT INTO completion_evaluations(
            project_id,contract_hash,plan_signature,achieved,payload,evaluated_at
        ) VALUES(?,?,?,?,?,?)""", ("p1", "c1", "s1", 0, "{}", "now"))
    GoalCompletionStore(memory_path)
    with store.connect() as db:
        assert db.execute("SELECT state FROM goal_states WHERE project_id='p1'").fetchone()[0] == "draft"
        assert db.execute("SELECT COUNT(*) FROM completion_evaluations WHERE project_id='p1'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM human_acceptances").fetchone()[0] == 0
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"goal_contracts", "plan_coverage", "completion_evaluations", "goal_states", "human_acceptances"} <= names


def test_missing_artifact_hash_cannot_be_accepted(manager):
    with pytest.raises(ValueError, match="成果物がない"):
        _accept(manager, artifact=False)
    assert _rows(GoalCompletionStore(manager.memory.path)) == []
    result = _evaluate(manager, artifact=False)
    assert result["achieved"] is False
    assert result["human_acceptance"]["valid"] is False
