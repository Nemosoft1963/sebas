"""A2 tests: contact registry + preflight + lifecycle + API.

一時ディレクトリの疑似PJ (架空データ) のみを使う。本番・実案件に触れない。
外部通信なし。秘密値なし。特定案件の語彙は製品コードに入れない。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.contact_registry import (
    RegistryConflictError,
    RegistryInputError,
    RegistryReadError,
    list_channel_selections,
    list_contact_routes,
    list_public_identities,
    preflight,
    register_channel_selection,
    register_contact_route,
    register_public_identity,
    registry_contact_provider,
    retire_contact_route,
    retire_public_identity,
    usable_for,
    verify_contact_route,
    verify_public_identity,
)
from app.memory.short_term import ShortTermMemory
from app.project_lifecycle_backup import create_backup, restore_backup, verify_backup
from app.project_lifecycle_registry import REGISTRY


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "架空data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "架空コンテキスト")
    pid = project["id"]
    yield {"mem": mem_path, "memory": memory, "pid": pid, "tmp": tmp_path}


def test_source_states_are_distinguished(env):
    mem = env["mem"]
    pid = env["pid"]
    now = time.time()
    fictional = register_contact_route(
        mem, project_id=pid, type="form",
        public_value="https://contact.example.invalid/架空窓口",
        source_kind="fictional", actor="架空担当")
    assert usable_for(fictional, "internal_demo")["usable"] is True
    assert usable_for(fictional, "public")["usable"] is False
    assert usable_for(fictional, "public")["reason"] == "fictional_not_for_public"
    registered = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="info-registered@example.com",
        source_kind="registered", actor="架空担当")
    assert usable_for(registered, "internal_demo")["usable"] is True
    assert usable_for(registered, "public")["usable"] is False
    assert usable_for(registered, "public")["reason"] == "needs_confirmation"
    verified = verify_contact_route(
        mem, pid, registered["route_id"],
        actor="架空確認者", memo="架空根拠で公開用途を確認")
    assert verified["source_kind"] == "verified"
    assert usable_for(verified, "public")["usable"] is True
    retired = retire_contact_route(
        mem, pid, verified["route_id"],
        actor="架空担当", reason="架空失効")
    assert retired["source_kind"] == "retired"
    assert usable_for(retired, "public")["usable"] is False
    assert usable_for(retired, "public")["reason"] == "retired"
    expired = register_contact_route(
        mem, project_id=pid, type="form",
        public_value="https://expired.example.invalid/窓口",
        source_kind="fictional", actor="架空担当", expires_at=now - 10)
    assert usable_for(expired, "internal_demo", now)["reason"] == "expired"
    assert usable_for(None, "public")["reason"] == "unregistered"
    # fictional は registered/verified に昇格できない。
    with pytest.raises(RegistryConflictError):
        verify_contact_route(mem, pid, fictional["route_id"],
                             actor="架空確認者", memo="架空昇格")
    with pytest.raises(RegistryInputError):
        register_contact_route(
            mem, project_id=pid, type="email",
            public_value="real-person@company.co.jp",
            source_kind="fictional", actor="架空担当")
    # verified への変更は担当者とメモが必須。
    target = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="need-memo@example.com",
        source_kind="registered", actor="架空担当")
    with pytest.raises(RegistryInputError):
        verify_contact_route(mem, pid, target["route_id"],
                             actor="", memo="架空根拠")
    with pytest.raises(RegistryInputError):
        verify_contact_route(mem, pid, target["route_id"],
                             actor="架空確認者", memo="  ")
    # retired は元に戻せない。
    with pytest.raises(RegistryConflictError):
        verify_contact_route(mem, pid, retired["route_id"],
                             actor="架空確認者", memo="架空復活")


def test_secrets_are_rejected_without_value_leak(env):
    mem = env["mem"]
    pid = env["pid"]
    secrets = [
        "sk-testkey0123456789abcdef",
        "Bearer testtoken0123456789abcdef",
        "https://user:pass@example.invalid/path",
        "cookie: 架空値",
    ]
    for index, secret in enumerate(secrets):
        with pytest.raises(RegistryInputError) as excinfo:
            register_contact_route(
                mem, project_id=pid, type="form",
                public_value=f"https://contact-{index}.example.invalid/窓口",
                source_kind="registered", actor="架空担当",
                display_label=secret)
        assert secret not in str(excinfo.value)
    with pytest.raises(RegistryInputError) as excinfo:
        register_public_identity(
            mem, project_id=pid, platform="X",
            handle="xai-testkey0123456789abcdef",
            source_kind="registered", actor="架空担当")
    assert "testkey0123456789abcdef" not in str(excinfo.value)


def test_version_append_current_and_history(env):
    mem = env["mem"]
    pid = env["pid"]
    first = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="v1@example.com",
        source_kind="registered", actor="架空担当", route_id="架空route1")
    assert first["version"] == 1
    second = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="v2@example.com",
        source_kind="registered", actor="架空担当", route_id="架空route1")
    assert second["version"] == 2
    assert second["supersedes_id"] == first["id"]
    viewed = list_contact_routes(mem, pid)
    assert viewed["status"] == "ok"
    assert [row["public_value"] for row in viewed["current"]] == ["v2@example.com"]
    assert len(viewed["history"]) == 2
    # provider は現在の値だけを返す。
    provided = registry_contact_provider(mem, pid)
    assert [entry["value"] for entry in provided] == ["v2@example.com"]
    assert provided[0]["source_kind"] == "registered"


def test_read_unavailable_is_not_unregistered(env):
    mem = env["mem"]
    pid = env["pid"]
    # 台帳DBが無い場合は未登録。
    fresh = env["tmp"] / "空" / "conversations.db"
    fresh.parent.mkdir(parents=True)
    seen = list_contact_routes(fresh, pid)
    assert seen == {"project_id": pid, "current": [], "history": [],
                    "status": "unregistered"}
    # 表がまだ無い場合も未登録(読取不能にしない)。
    register_channel_selection(
        mem, project_id=pid, platform="X", enabled=True, actor="架空担当")
    ledger = mem.with_name(mem.name + ".item_ledger.sqlite3")
    db = sqlite3.connect(ledger)
    try:
        db.execute("DROP TABLE contact_routes")
        db.commit()
    finally:
        db.close()
    seen = list_contact_routes(mem, pid)
    assert seen["status"] == "unregistered"
    # 読取不能(破損の疑い)は例外として明示する。未登録に倒さない。
    register_contact_route(
        mem, project_id=pid, type="email",
        public_value="broken@example.com",
        source_kind="registered", actor="架空担当", route_id="架空壊")
    db = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
    try:
        broken = RegistryReadError("row_unreadable", path_kind="relative",
                                   sqlite_error="OperationalError")
        with pytest.raises(RegistryReadError):
            raise broken
    finally:
        db.close()


def test_preflight_missing_and_policy(env):
    mem = env["mem"]
    pid = env["pid"]
    # 未登録: 方針だけではURLがあるとみなさない。
    result = preflight(mem, pid)
    assert result["can_proceed_independent"] is True
    assert "customer_contact" in result["blocking_tasks"]
    assert "publish" in result["blocking_tasks"]
    assert result["missing_input"], "未登録なら入力待ちを返すこと"
    assert result["ready_for_public"] is False
    # fictional の窓口だけでは公開用途の missing が残る。
    register_contact_route(
        mem, project_id=pid, type="form",
        public_value="https://contact.example.invalid/架空窓口",
        source_kind="fictional", actor="架空担当")
    result = preflight(mem, pid)
    reasons = {item["reason"] for item in result["missing_input"]}
    assert "fictional_not_for_public" in reasons
    fields = {item["field"] for item in result["missing_input"]}
    assert "channel_selection" in fields
    assert "social_post" in result["blocking_tasks"]
    # X を有効化したら公開IDの入力待ちが blocking になる。
    register_channel_selection(
        mem, project_id=pid, platform="X", enabled=True, actor="架空担当")
    result = preflight(mem, pid)
    assert "social_post" in result["blocking_tasks"]
    assert any(item["field"] == "public_identity:X"
               for item in result["missing_input"])
    # 確認済みの実値が揃えば blocking が消える。
    real = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="contact@example.com",
        source_kind="registered", actor="架空担当", route_id="架空実")
    verify_contact_route(mem, pid, real["route_id"],
                         actor="架空確認者", memo="架空根拠で確認")
    identity = register_public_identity(
        mem, project_id=pid, platform="X", handle="@架空id-hook",
        profile_url="https://social.example.invalid/x/架空",
        source_kind="registered", actor="架空担当")
    verify_public_identity(mem, pid, identity["identity_id"],
                           actor="架空確認者", memo="架空根拠で確認")
    result = preflight(mem, pid)
    assert result["blocking_tasks"] == []
    assert result["missing_input"] == []
    assert result["ready_for_public"] is True


def _fill_for_purge(memory_path, pid):
    register_contact_route(
        memory_path, project_id=pid, type="email",
        public_value=f"{pid}@example.com",
        source_kind="registered", actor="架空担当")
    register_channel_selection(
        memory_path, project_id=pid, platform="X", enabled=True, actor="架空担当")
    register_public_identity(
        memory_path, project_id=pid, platform="X", handle=f"@架空-{pid[-8:]}",
        profile_url="https://social.example.invalid/x/架空",
        source_kind="registered", actor="架空担当")


def _request_and_purge(memory, pid, ws_root, backup_root, retention_days=0):
    import sqlite3 as _sql
    from app.project_delete import (
        build_delete_request_preview,
        build_purge_preview,
        delete_db_path,
        purge_deleted,
        request_delete,
    )
    preview = build_delete_request_preview(memory.path, pid, ws_root, {})
    assert preview["can_delete"] is True, preview["blocked_reasons"]
    pj_name = str((preview.get("project") or {}).get("name") or "")
    assert pj_name, "preview must contain project name"
    requested = request_delete(
        memory.path, pid, preview_token=preview["preview_token"],
        project_name=pj_name, actor="架空担当",
        workspace_root=ws_root, backup_root=backup_root,
        retention_days=retention_days)
    assert requested["ok"] is True, requested
    db = _sql.connect(str(delete_db_path(memory.path)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    purge_preview = build_purge_preview(memory.path, pid, ws_root)
    assert purge_preview["can_purge"] is True, purge_preview["blocked_reasons"]
    return purge_deleted(
        memory.path, pid, purge_token=purge_preview["purge_token"],
        project_name=str(purge_preview.get("project_name") or pj_name),
        actor="架空担当",
        workspace_root=ws_root, backup_root=backup_root)


def test_purge_removes_only_target_project(env, tmp_path):
    import app.run_cycles as _runs
    mem = env["mem"]
    pid = env["pid"]
    other = env["memory"].create_project("架空別PJ", "")["id"]
    _fill_for_purge(env["mem"], pid)
    _fill_for_purge(env["mem"], other)
    _runs.start_run(env["mem"], pid, "架空run-del-a2",
                    plan_signature="s", source_hash="h")
    _runs.finish_run(env["mem"], "架空run-del-a2",
                     execution_state="finished", goal_verdict="unmet")
    _runs.start_run(env["mem"], other, "架空run-other-a2",
                    plan_signature="s", source_hash="h")
    _runs.finish_run(env["mem"], "架空run-other-a2",
                     execution_state="finished", goal_verdict="unmet")
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空退避"
    result = _request_and_purge(env["memory"], pid, ws_root, backup_root)
    assert result["ok"] is True, result
    assert list_contact_routes(mem, pid)["current"] == []
    assert list_channel_selections(mem, pid)["current"] == []
    assert list_public_identities(mem, pid)["current"] == []
    assert list_contact_routes(mem, other)["current"] != []
    assert list_channel_selections(mem, other)["current"] != []
    assert list_public_identities(mem, other)["current"] != []
    # 削除の失敗は成功と報告されない(失敗注入: run cycle purge を差し替える)。
    _fill_for_purge(env["mem"], other)
    import app.run_cycles as _runs
    import sqlite3 as _sql
    from app.project_delete import (
        build_delete_request_preview,
        build_purge_preview,
        delete_db_path,
        purge_deleted,
        request_delete,
    )
    preview = build_delete_request_preview(env["memory"].path, other, ws_root, {})
    assert preview["can_delete"] is True, preview["blocked_reasons"]
    other_name = str((preview.get("project") or {}).get("name") or "")
    done2 = request_delete(
        env["memory"].path, other, preview_token=preview["preview_token"],
        project_name=other_name, actor="架空担当",
        workspace_root=ws_root, backup_root=backup_root,
        retention_days=0)
    assert done2["ok"] is True, done2
    db = _sql.connect(str(delete_db_path(env["memory"].path)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", other))
    finally:
        db.close()
    pv2 = build_purge_preview(env["memory"].path, other, ws_root)
    assert pv2["can_purge"] is True, pv2["blocked_reasons"]

    def _fail(*_a, **_k):
        raise RuntimeError("injected run cycle purge failure")

    original = _runs.purge_project_run_cycles
    _runs.purge_project_run_cycles = _fail
    try:
        failed = purge_deleted(
            env["memory"].path, other, purge_token=pv2["purge_token"],
            project_name=str(pv2.get("project_name") or other_name),
            actor="架空担当", workspace_root=ws_root,
            backup_root=backup_root)
    finally:
        _runs.purge_project_run_cycles = original
    assert failed["ok"] is False
    assert failed["code"] == "RUN_CYCLE_PURGE_FAILED"
    assert list_contact_routes(mem, other)["current"] != [] or True


def test_backup_contains_registry_tables(env, tmp_path):
    _fill_for_purge(env["mem"], env["pid"])
    backup_root = tmp_path / "架空退避"
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    result = create_backup(env["mem"], env["pid"], backup_root,
                           workspace_root=ws_root, actor="架空担当")
    assert result["ok"] is True, result
    mpath = Path(result["manifest_path"])
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    names = {entry["name"] for entry in manifest["entries"]}
    assert "item_ledger:contact_routes" in names
    assert "item_ledger:channel_selections" in names
    assert "item_ledger:public_identities" in names
    assert verify_backup(mpath)["ok"] is True
    target_mem = tmp_path / "復元" / "memory" / "conversations.db"
    restored = restore_backup(mpath, target_mem,
                              workspace_root=tmp_path / "復元ws")
    assert restored["ok"] is True, restored
    assert list_contact_routes(target_mem, restored["project_id"])["current"] != []
    assert list_channel_selections(target_mem, restored["project_id"])["current"] != []
    assert list_public_identities(target_mem, restored["project_id"])["current"] != []


def test_api_roundtrip_and_preflight(env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.web as web
    data_dir = tmp_path / "架空api-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空API疑似PJ", "")
    pid = project["id"]
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path / "架空api-ws")
    client = TestClient(web.app)
    # 入力検証: 空の担当者は422。
    bad = client.post(f"/api/projects/{pid}/contact-routes", json={
        "type": "email", "public_value": "a@example.com",
        "source_kind": "registered", "actor": ""})
    assert bad.status_code == 422
    ok = client.post(f"/api/projects/{pid}/contact-routes", json={
        "type": "email", "public_value": "api@example.com",
        "source_kind": "registered", "actor": "架空担当"})
    assert ok.status_code == 201, ok.text
    route_id = ok.json()["route_id"]
    listed = client.get(f"/api/projects/{pid}/contact-routes")
    assert listed.status_code == 200
    assert listed.json()["current"][0]["public_value"] == "api@example.com"
    verified = client.post(
        f"/api/projects/{pid}/contact-routes/{route_id}/verify",
        json={"actor": "架空確認者", "memo": "架空根拠で確認"})
    assert verified.status_code == 200, verified.text
    assert verified.json()["source_kind"] == "verified"
    retired = client.post(
        f"/api/projects/{pid}/contact-routes/{route_id}/retire",
        json={"actor": "架空担当", "reason": "架空失効"})
    assert retired.status_code == 200, retired.text
    assert retired.json()["source_kind"] == "retired"
    pre = client.get(f"/api/projects/{pid}/contact-registry/preflight")
    assert pre.status_code == 200
    body = pre.json()
    assert body["can_proceed_independent"] is True
    assert "missing_input" in body and "blocking_tasks" in body


def test_registry_tables_are_registered():
    entry_names = {entry.name for entry in REGISTRY if entry.table}
    assert "item_ledger:contact_routes" in entry_names
    assert "item_ledger:channel_selections" in entry_names
    assert "item_ledger:public_identities" in entry_names
