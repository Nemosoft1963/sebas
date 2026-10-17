"""C-4 関門と有効化APIのテスト (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
実案件のPJのデータには書込まない (一時ディレクトリの疑似PJのみ)。
"""
import sqlite3
import time
import uuid

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox


def _make_manager(tmp_path, tag="gate"):
    name = "c4-疑似-関門-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の原本引用つき作成と検証",
                "架空文書02の原本引用つき作成と検証"]
    memory.save_mission(pid, "架空目標-関門", "\n".join(
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


def _insert_direct(manager, pid, sig, label="", danger=0, at=None):
    import app.shadow_mode as shadow
    db_path = shadow.shadow_db_path(manager)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=30)
    try:
        with db:
            shadow._ensure_tables(db)
            rid = "sh_" + uuid.uuid4().hex[:16]
            now = float(at) if at is not None else time.time()
            idem = shadow._short_hash([pid, sig, "record_stop_class",
                                       "missing_decision", "x:y", rid])
            db.execute(
                """INSERT INTO shadow_records(id, project_id, plan_signature,
                       observed_at, actor, planned_action, stop_class,
                       stop_code, gate_status, preflight_status,
                       next_step_kind, reason_ja, label, label_by, label_at,
                       label_note, danger, idempotency_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, pid, sig, now, "架空担当", "record_stop_class",
                 "missing_decision", "x:y", "ok", "ok", "", "架空理由",
                 label, "架空判定者" if label else "",
                 now if label else 0.0, "", int(danger), idem))
            return rid
    finally:
        db.close()


def _fill_passing(manager, pid):
    sigs = ["sig-1", "sig-2", "sig-3"]
    for i in range(19):
        _insert_direct(manager, pid, sigs[i % 3], label="correct")
    _insert_direct(manager, pid, "sig-3", label="wrong")


def _api_client(monkeypatch, manager):
    import app.web as web
    from fastapi.testclient import TestClient
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    return TestClient(web.app, raise_server_exceptions=False)


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


def test_enable_blocked_when_gate_fails(tmp_path, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, tag="blocked")
    client = _api_client(monkeypatch, manager)
    assert auto.is_enabled(manager, pid) is False
    resp = client.post(f"/api/projects/{pid}/autopilot/enable",
                       json={"actor": "架空担当", "reason": "架空の有効化理由"})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    assert isinstance(detail, dict) and "gate" in detail
    assert detail["gate"]["passed"] is False
    assert "影モード" in detail["hint"]
    assert auto.is_enabled(manager, pid) is False


def test_enable_passes_when_gate_passes(tmp_path, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, tag="pass")
    _fill_passing(manager, pid)
    client = _api_client(monkeypatch, manager)
    resp = client.post(f"/api/projects/{pid}/autopilot/enable",
                       json={"actor": "架空担当", "reason": "架空の有効化理由"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert auto.is_enabled(manager, pid) is True


def test_override_rules_and_audit(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="override")
    client = _api_client(monkeypatch, manager)
    # 10字未満・actor なしは拒否。
    short = client.post(f"/api/projects/{pid}/autopilot/enable",
                        json={"actor": "架空担当", "reason": "架空の有効化理由",
                              "gate_override_reason": "短い理由"})
    assert short.status_code == 409
    assert auto.is_enabled(manager, pid) is False
    no_actor = client.post(f"/api/projects/{pid}/autopilot/enable",
                           json={"actor": "", "reason": "架空の有効化理由",
                                 "gate_override_reason": "架空の十分に長い理由です"})
    assert no_actor.status_code == 422
    assert auto.is_enabled(manager, pid) is False
    # 正しいオーバーライドで有効化。
    ok = client.post(f"/api/projects/{pid}/autopilot/enable",
                     json={"actor": "架空担当", "reason": "架空の有効化理由",
                           "gate_override_reason": "架空の十分に長い理由です"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["enabled"] is True
    overrides = shadow.list_overrides(manager, pid)
    assert len(overrides) == 1
    assert overrides[0]["actor"] == "架空担当"
    assert "架空の十分に長い理由です" in overrides[0]["reason"]
    assert "autopilot_enabled_without_gate" in _event_kinds(manager, pid)


def test_gate_check_failure_does_not_enable(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="gatefail")
    _fill_passing(manager, pid)
    client = _api_client(monkeypatch, manager)
    shadow.FAIL_INJECT = {"stage": "gate_unreadable"}
    try:
        resp = client.post(f"/api/projects/{pid}/autopilot/enable",
                           json={"actor": "架空担当", "reason": "架空の有効化理由"})
        assert resp.status_code == 409, resp.text
        assert auto.is_enabled(manager, pid) is False
    finally:
        shadow.FAIL_INJECT = None


def test_gate_unreadable_cannot_be_overridden(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="unreadable-override")
    client = _api_client(monkeypatch, manager)
    shadow.FAIL_INJECT = {"stage": "gate_unreadable"}
    try:
        resp = client.post(f"/api/projects/{pid}/autopilot/enable",
                           json={"actor": "架空担当", "reason": "架空の有効化理由",
                                 "gate_override_reason": "架空の十分に長い理由です"})
        assert resp.status_code == 409, resp.text
        assert auto.is_enabled(manager, pid) is False
        assert shadow.list_overrides(manager, pid) == []
    finally:
        shadow.FAIL_INJECT = None


def test_gate_exception_cannot_be_overridden(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="exception-override")
    client = _api_client(monkeypatch, manager)
    monkeypatch.setattr(shadow, "gate", lambda *_a: (_ for _ in ()).throw(OSError("unreadable")))
    resp = client.post(f"/api/projects/{pid}/autopilot/enable",
                       json={"actor": "架空担当", "reason": "架空の有効化理由",
                             "gate_override_reason": "架空の十分に長い理由です"})
    assert resp.status_code == 409, resp.text
    assert auto.is_enabled(manager, pid) is False
    assert shadow.list_overrides(manager, pid) == []


def test_set_enabled_direct_unchanged_and_disable_untouched(tmp_path, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, tag="direct")
    # set_enabled 本体は関門を見ない (直接呼べば従来どおり)。
    cfg = auto.set_enabled(manager, pid, True, actor="架空担当",
                           reason="架空の有効化理由")
    assert cfg["enabled"] is True
    client = _api_client(monkeypatch, manager)
    # disable は関門の影響を受けない。
    disabled = client.post(f"/api/projects/{pid}/autopilot/disable",
                           json={"actor": "架空担当", "reason": "架空理由"})
    assert disabled.status_code == 200
    assert auto.is_enabled(manager, pid) is False
    # disable-all・キルスイッチは影響を受けない。
    stopped = client.post("/api/autopilot/disable-all",
                          json={"actor": "架空担当"})
    assert stopped.status_code == 200
    kill = client.get("/api/autopilot/kill-switch")
    assert kill.status_code == 200
    released = client.post("/api/autopilot/kill-switch/release",
                           json={"actor": "架空担当"})
    assert released.status_code == 200


def test_shadow_api_crud_and_keys(tmp_path, monkeypatch):
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="api")
    client = _api_client(monkeypatch, manager)
    # observe は actor 必須。
    assert client.post(f"/api/projects/{pid}/shadow/observe",
                       json={}).status_code == 422
    observed = client.post(f"/api/projects/{pid}/shadow/observe",
                           json={"actor": "架空担当"})
    assert observed.status_code == 200, observed.text
    assert observed.json()["ok"] is True
    rid = observed.json()["record_id"]
    # records。
    records = client.get(f"/api/projects/{pid}/shadow/records?limit=10")
    assert records.status_code == 200
    assert len(records.json()["records"]) == 1
    # label は actor・label 必須。danger は誤りのときだけ。
    assert client.post(f"/api/shadow/records/{rid}/label",
                       json={"actor": "架空担当", "label": "nope"}).status_code == 422
    bad_danger = client.post(f"/api/shadow/records/{rid}/label",
                             json={"actor": "架空担当", "label": "correct",
                                   "danger": True})
    assert bad_danger.status_code == 422
    labeled = client.post(f"/api/shadow/records/{rid}/label",
                          json={"actor": "架空判定者", "label": "correct",
                                "note": "架空メモ"})
    assert labeled.status_code == 200, labeled.text
    assert labeled.json()["record"]["label"] == "correct"
    missing = client.post("/api/shadow/records/sh_存在しない/label",
                          json={"actor": "架空判定者", "label": "correct"})
    assert missing.status_code == 404
    # metrics・gate。
    metrics = client.get(f"/api/projects/{pid}/shadow/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["count"] == 1
    gate = client.get(f"/api/projects/{pid}/shadow/gate")
    assert gate.status_code == 200
    assert gate.json()["passed"] is False
    # observe-all は actor 必須。
    assert client.post("/api/shadow/observe-all", json={}).status_code == 422
    all_resp = client.post("/api/shadow/observe-all",
                           json={"actor": "架空担当"})
    assert all_resp.status_code == 200, all_resp.text
    assert all_resp.json()["ok"] is True
    # summary。
    summary = client.get("/api/shadow/summary")
    assert summary.status_code == 200
    assert summary.json()["total"] >= 1
    assert shadow.list_records(manager, pid)
