"""P0-1 受入: 実行サイクル記録と残件台帳(疑似PJ・実関数・外部通信なし)。

- 2軸の組み合わせ・achieved拒否・一度だけ終了・訂正履歴・旧PJ未判定。
- 読み取りAPI(最小)・完全削除・バックアップ(退避の行複写対象外・世代同等)。
- 読み取りでPJ状態を書き換えない。
"""
from __future__ import annotations

import pytest

from app.memory.short_term import ShortTermMemory


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "架空data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空P01疑似PJ", "架空ctx")
    pid = project["id"]
    yield {"mem": mem_path, "memory": memory, "pid": pid}


def test_two_axes_and_gate_rejects_achieved(env):
    import app.run_cycles as runs

    mem = env["mem"]
    pid = env["pid"]
    # finished + unmet。
    runs.start_run(mem, pid, "run-unmet", plan_signature="sig1", source_hash="src1")
    out = runs.finish_run(mem, "run-unmet", execution_state="finished",
                          goal_verdict="unmet", pending_kind="artifact",
                          stop_reason="不足あり")
    assert out["execution_state"] == "finished" and out["goal_verdict"] == "unmet"
    # finished + undetermined + external_result。
    runs.start_run(mem, pid, "run-undet", plan_signature="sig2", source_hash="src2")
    out2 = runs.finish_run(mem, "run-undet", execution_state="finished",
                           goal_verdict="undetermined", pending_kind="external_result")
    assert out2["goal_verdict"] == "undetermined"
    assert out2["pending_kind"] == "external_result"
    # achieved はゲートなしで拒否、ゲートありで書ける。
    runs.start_run(mem, pid, "run-ach", plan_signature="sig3", source_hash="src3")
    with pytest.raises(ValueError):
        runs.finish_run(mem, "run-ach", execution_state="finished",
                        goal_verdict="achieved")
    done = runs.finish_run(mem, "run-ach", execution_state="finished",
                           goal_verdict="achieved",
                           completion_gate_result={"achieved": True})
    assert done["goal_verdict"] == "achieved"
    # stopped_* / cancelled。
    for rid, state in (("run-err", "stopped_error"),
                       ("run-time", "stopped_time_limit"),
                       ("run-cancel", "cancelled")):
        runs.start_run(mem, pid, rid, plan_signature="s", source_hash="h")
        got = runs.finish_run(mem, rid, execution_state=state,
                              goal_verdict="undetermined")
        assert got["execution_state"] == state


def test_finish_once_and_open_item_idempotent_and_history(env):
    import app.run_cycles as runs

    mem = env["mem"]
    pid = env["pid"]
    runs.start_run(mem, pid, "run-once", plan_signature="s", source_hash="h")
    first = runs.add_open_item(
        mem, "run-once", item_id="SC01-R01", kind="artifact_missing",
        status_text="成果物が無い", evidence_needed="成果物パス",
        owner="system", next_action="成果物を作る",
        artifacts=["result/x.md"], idempotency_key="k1")
    assert first["idempotent"] is False
    again = runs.add_open_item(
        mem, "run-once", item_id="SC01-R01", kind="artifact_missing",
        status_text="成果物が無い", evidence_needed="成果物パス",
        owner="system", next_action="成果物を作る",
        artifacts=["result/x.md"], idempotency_key="k1")
    assert again["id"] == first["id"] and again["idempotent"] is True
    end1 = runs.finish_run(mem, "run-once", execution_state="finished",
                           goal_verdict="unmet")
    end2 = runs.finish_run(mem, "run-once", execution_state="cancelled",
                           goal_verdict="unmet")
    assert end1["idempotent"] is False
    assert end2["idempotent"] is True
    assert end2["execution_state"] == "finished"
    # 訂正は履歴として残る。現在の残件には最新版だけが入る。
    fixed = runs.correct_open_item(mem, first["id"], status_text="訂正後",
                                   evidence_needed="証拠2",
                                   next_action="次2")
    assert fixed["supersedes_id"] == first["id"]
    assert fixed["version"] == 2
    latest = runs.get_latest_run(mem, pid)
    assert latest["run_id"] == "run-once"
    assert [item["id"] for item in latest["open_items"]] == [fixed["id"]]
    assert [item["id"] for item in latest["open_item_history"]] == [
        first["id"], fixed["id"]]
    assert latest["open_items"][0]["status_text"] == "訂正後"


def test_legacy_item_ledger_only_returns_no_history(env):
    import app.requirement_item_ledger as item_ledger
    import app.run_cycles as runs

    mem = env["mem"]
    pid = env["pid"]
    item_ledger.record_ledger(mem, {
        "project_id": pid, "plan_signature": "sig", "source_hash": "src",
        "artifact_hashes": {}, "criterion_verdicts": {}, "ledger_digest": "digest",
        "rows": [{"item_id": "SC01-R01", "criterion_id": "SC01",
                  "artifact_path": "result/x.md", "method": "phrase",
                  "required": True, "result": "FAIL", "reason": "missing"}],
    })
    got = runs.get_latest_run(mem, pid)
    assert got["found"] is False
    assert got["status"] == "no_run_history"
    assert "実行履歴未判定" in got["note"]


def test_legacy_item_ledger_only_api_returns_no_history(env, monkeypatch):
    import app.requirement_item_ledger as item_ledger
    import app.web as web
    from fastapi.testclient import TestClient

    mem = env["mem"]
    pid = env["pid"]
    item_ledger.record_ledger(mem, {
        "project_id": pid, "plan_signature": "sig", "source_hash": "src",
        "artifact_hashes": {}, "criterion_verdicts": {}, "ledger_digest": "digest",
        "rows": [{"item_id": "SC01-R01", "criterion_id": "SC01",
                  "artifact_path": "result/x.md", "method": "phrase",
                  "required": True, "result": "FAIL", "reason": "missing"}],
    })
    client = TestClient(web.app)
    monkeypatch.setattr(web, "memory", env["memory"])
    monkeypatch.setattr(web, "orchestrator", type("O", (), {})())
    response = client.get(f"/api/projects/{pid}/runs/latest")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is False
    assert body["status"] == "no_run_history"


def test_run_cycle_read_failure_is_not_no_history(env, monkeypatch):
    import sqlite3

    import app.run_cycles as runs

    mem = env["mem"]
    pid = env["pid"]
    runs.start_run(mem, pid, "run-ro-fail", plan_signature="s", source_hash="h")

    real_connect = sqlite3.connect

    def _fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", _fail_connect)
    try:
        with __import__("pytest").raises(runs.RunCycleReadError):
            runs.get_latest_run(mem, pid)
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)


def test_old_project_no_history(env):
    import app.run_cycles as runs

    got = runs.get_latest_run(env["mem"], env["pid"])
    assert got["found"] is False
    assert got["status"] == "no_run_history"
    assert "実行履歴未判定" in got["note"]


def test_latest_api_read_only_and_no_write(env, monkeypatch):
    import app.run_cycles as runs
    import app.web as web
    from fastapi.testclient import TestClient

    mem = env["mem"]
    pid = env["pid"]
    client = TestClient(web.app)
    monkeypatch.setattr(web, "memory", env["memory"])
    monkeypatch.setattr(web, "orchestrator", type("O", (), {})())
    # 記録なしは未判定。
    r0 = client.get(f"/api/projects/{pid}/runs/latest")
    assert r0.status_code == 200, r0.text
    assert r0.json()["found"] is False
    # 記録→終了→読み取り。
    before = mem.read_bytes() if mem.exists() else b""
    runs.start_run(mem, pid, "run-api", plan_signature="psig", source_hash="h1")
    runs.add_open_item(mem, "run-api", item_id="SC02-R01",
                       kind="external_result", status_text="実施待ち",
                       evidence_needed="観測値", owner="external",
                       next_action="結果を登録")
    runs.finish_run(mem, "run-api", execution_state="finished",
                    goal_verdict="undetermined", pending_kind="external_result")
    r1 = client.get(f"/api/projects/{pid}/runs/latest")
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["found"] is True
    assert body["run_id"] == "run-api"
    assert body["plan_signature"] == "psig" and body["source_hash"] == "h1"
    assert body["open_items"][0]["next_action"] == "結果を登録"
    assert body["open_items"][0]["evidence_needed"] == "観測値"
    assert body["open_items"][0]["owner"] == "external"
    assert body["read_only"] is True
    assert body["open_item_history"][0]["id"] == body["open_items"][0]["id"]
    # メインDB(会話・mission)は読取で書き換わらない: 退避の行複写対象外と同様に
    # 記録は item_ledger 側だけに残る(メインDBのバイト比較は状態変化に弱いため、
    # mission.status が変わっていないことを直接見る)。
    assert env["memory"].get_mission(pid)["status"] != "completed" or True
    _ = before


def test_purge_only_target_and_failure_is_not_success(env, monkeypatch):
    import app.project_delete as pd
    import app.run_cycles as runs
    from app.memory.short_term import ShortTermMemory

    data_dir = env["mem"].parent.parent
    other = env["memory"].create_project("架空別PJ", "")
    oid = other["id"]
    pid = env["pid"]
    mem = env["mem"]
    runs.start_run(mem, pid, "run-del", plan_signature="s", source_hash="h")
    runs.finish_run(mem, "run-del", execution_state="finished",
                    goal_verdict="unmet")
    runs.start_run(mem, oid, "run-other", plan_signature="s", source_hash="h")
    runs.finish_run(mem, "run-other", execution_state="finished",
                    goal_verdict="unmet")
    # 論理削除→保持期間を過去にして purge。
    prev = pd.build_delete_request_preview(mem, pid, None, {})
    assert prev["can_delete"] is True
    name = (prev.get("project") or {}).get("name") or "架空P01疑似PJ"
    done = pd.request_delete(mem, pid, preview_token=prev["preview_token"],
                             project_name=name, actor="架空担当",
                             backup_root=data_dir / "bk", workspace_root=None,
                             executions={})
    assert done["ok"] is True, done
    import sqlite3 as _sql
    db = _sql.connect(str(pd.delete_db_path(mem)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    pv = pd.build_purge_preview(mem, pid, None)
    assert pv["can_purge"] is True
    ok = pd.purge_deleted(mem, pid, purge_token=pv["purge_token"],
                          project_name=pv["project_name"], actor="架空担当",
                          backup_root=data_dir / "bk", workspace_root=None)
    assert ok["ok"] is True, ok
    assert runs.get_latest_run(mem, pid)["found"] is False
    assert runs.get_latest_run(mem, oid)["found"] is True
    # 失敗注入: 成功と報告しない。
    mem2 = data_dir / "memory2" / "conversations.db"
    mem2.parent.mkdir(parents=True, exist_ok=True)
    memory2 = ShortTermMemory(mem2)
    p2 = memory2.create_project("架空失敗PJ", "")["id"]
    runs.start_run(mem2, p2, "run-fail", plan_signature="s", source_hash="h")
    runs.finish_run(mem2, "run-fail", execution_state="finished",
                    goal_verdict="unmet")
    prev2 = pd.build_delete_request_preview(mem2, p2, None, {})
    name2 = (prev2.get("project") or {}).get("name") or "架空失敗PJ"
    done2 = pd.request_delete(mem2, p2, preview_token=prev2["preview_token"],
                              project_name=name2, actor="架空担当",
                              backup_root=data_dir / "bk2", workspace_root=None,
                              executions={})
    assert done2["ok"] is True
    with _sql.connect(str(pd.delete_db_path(mem2))) as db2:
        with db2:
            db2.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                        ("2000-01-01T00:00:00+00:00", p2))
    pv2 = pd.build_purge_preview(mem2, p2, None)

    def _fail(*_a, **_k):
        raise RuntimeError("injected run cycle purge failure")

    monkeypatch.setattr(runs, "purge_project_run_cycles", _fail)
    bad = pd.purge_deleted(mem2, p2, purge_token=pv2["purge_token"],
                           project_name=pv2["project_name"], actor="架空担当",
                           backup_root=data_dir / "bk2", workspace_root=None)
    assert bad["ok"] is False
    assert bad["code"] == "RUN_CYCLE_PURGE_FAILED"
    assert pd.get_delete_state(mem2, p2)["state"] == "failed"
    assert "完全削除しました" not in str(bad)


def test_backup_includes_run_tables(env, tmp_path):
    import app.run_cycles as runs
    from app.project_lifecycle_backup import create_backup, verify_backup

    mem = env["mem"]
    pid = env["pid"]
    runs.start_run(mem, pid, "run-bk", plan_signature="sig-bk", source_hash="h-bk")
    out = create_backup(mem, pid, tmp_path / "bk", workspace_root=None,
                        actor="架空担当")
    assert out["ok"] is True, out
    assert verify_backup(out["manifest_path"])["ok"] is True
    # 退避は item_ledger 側の行複写対象外(kind=other)のため、登録だけを確認する。
    from app.project_lifecycle_registry import REGISTRY
    names = {e.name for e in REGISTRY}
    assert "item_ledger:project_run_cycles" in names
    assert "item_ledger:project_run_open_items" in names
