"""D-1b 指示パッケージの取得API (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
Jenkinsへの接続・送信はしない。特定PJの語・ID・件数に依存しない。
"""
import hashlib
import json
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox


def _criteria(n):
    out = []
    for i in range(1, n + 1):
        if i % 3 == 0:
            out.append(f"架空顧客へ送信{i:02d}の実行と検証")
        else:
            out.append(f"架空告知文書{i:02d}の作成と検証")
    return out


def _make_manager(tmp_path, tag):
    name = "d1-疑似-運搬-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria(3)
    memory.save_mission(pid, "架空目標-運搬", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        if i % 3 == 0:
            title, scope = f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。"
        else:
            title, scope = f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成する。"
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


def _make_exported(tmp_path, tag, status="export_ready"):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, tag)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"]
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    target = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    base_ref = export.get_base_ref()
    assert base_ref and base_ref != "unknown"
    exported = export.export_proposal(manager, pid, target,
                                      actor="架空担当", base_ref=base_ref)
    assert exported["ok"] is True
    assert exported["status"] == "export_ready"
    if status == "submitted":
        import app.capability_delivery as delivery
        submitted = delivery.record_submitted(manager, pid, target,
                                              actor="架空担当")
        assert submitted["status"] == "submitted"
    return manager, pid, target, base_ref


def _package_dir(manager, pid, proposal_id):
    project = manager.memory.get_project(pid)
    _, scope = manager.workspace.project_path(
        project.get("workspace_path") or "", pid)
    return scope / "development_instructions" / proposal_id


def _db_snapshot(manager):
    import sqlite3
    from app.capability_gap import gap_db_path
    path = gap_db_path(manager)
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in sorted(tables):
            try:
                out[table] = db.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            except sqlite3.Error:
                out[table] = "確認できず"
        return out
    finally:
        db.close()


def _file_snapshot(base):
    out = {}
    if not base.exists():
        return out
    for child in sorted(base.rglob("*")):
        if child.is_file() and not child.is_symlink():
            out[child.relative_to(base).as_posix()] = child.read_bytes()
    return out


@pytest.mark.parametrize("status", ["export_ready", "submitted"])
def test_package_returns_full_texts_and_hashes(tmp_path, status):
    import app.capability_export as export
    manager, pid, target, base_ref = _make_exported(tmp_path, "全文-" + status,
                                                    status=status)
    result = export.read_package(manager, pid, target)
    assert result["ok"] is True
    assert result["proposal_id"] == target
    assert result["status"] == status
    assert result["base_ref"] == base_ref
    base = _package_dir(manager, pid, target)
    for name in ("instruction.json", "REQUEST.md", "manifest.json"):
        assert result["files"][name] == (base / name).read_text(encoding="utf-8")
        assert result["sha256"][name] == hashlib.sha256(
            (base / name).read_bytes()).hexdigest()
    # 応答に秘密値・内部の絶対パスを入れない (相対名とハッシュのみ)。
    blob = json.dumps(result, ensure_ascii=False)
    assert str(base) not in blob
    assert "/tmp" not in blob and "C:" not in blob
    assert "package_rel" not in result
    assert set(result["files"]) == {"instruction.json", "REQUEST.md",
                                    "manifest.json"}
    assert set(result["sha256"]) == {"instruction.json", "REQUEST.md",
                                     "manifest.json"}


def test_package_writes_nothing(tmp_path):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid, target, _base = _make_exported(tmp_path, "不変")
    base = _package_dir(manager, pid, target)
    before_db = _db_snapshot(manager)
    before_files = _file_snapshot(base)
    before_status = gap.get_proposal(manager, target)["status"]
    before_events = len(gap.list_proposal_events(manager, target))
    result = export.read_package(manager, pid, target)
    assert result["ok"] is True
    assert _db_snapshot(manager) == before_db
    assert _file_snapshot(base) == before_files
    assert gap.get_proposal(manager, target)["status"] == before_status
    assert len(gap.list_proposal_events(manager, target)) == before_events
    # 失敗時も何も書き込まない (欠落させた後の409で確認)。
    (base / "REQUEST.md").unlink()
    failed = export.read_package(manager, pid, target)
    assert failed["ok"] is False
    assert "files" not in failed
    assert _db_snapshot(manager) == before_db
    assert gap.get_proposal(manager, target)["status"] == before_status


@pytest.mark.parametrize("status", ["draft", "reviewed", "delivered",
                                    "verified", "rejected", "stale",
                                    "needs_evidence"])
def test_package_rejects_other_statuses(tmp_path, status):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "状態-" + status)
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    target = out["proposals"][0]["proposal_id"]
    base_ref = export.get_base_ref()
    if status in ("reviewed", "export_ready", "submitted", "delivered"):
        gap.set_proposal_status(manager, pid, target, "reviewed",
                                actor="架空担当", reason="架空確認")
    if status in ("export_ready", "submitted", "delivered"):
        exported = export.export_proposal(manager, pid, target,
                                          actor="架空担当", base_ref=base_ref)
        assert exported["ok"] is True
    if status in ("submitted", "delivered"):
        submitted = delivery.record_submitted(manager, pid, target,
                                              actor="架空担当")
        assert submitted["status"] == "submitted"
    if status == "delivered":
        diff = "架空差分\n+架空実装\n"
        payload = {
            "base_commit": base_ref,
            "result_commit": "架空成果",
            "diff_text": diff,
            "artifact_sha256": hashlib.sha256(
                delivery._canonical_delivery_content(
                    diff, "", None)).hexdigest(),
            "test_results": {"total": 1, "failed": 0, "passed": True},
            "capability_ids": [],
            "failure_logs": "架空失敗なし",
        }
        accepted = delivery.record_delivery(manager, pid, target, payload,
                                            actor="架空担当")
        assert accepted["verdict"] == "delivered"
    if status not in ("export_ready", "submitted"):
        result = export.read_package(manager, pid, target)
        assert result["ok"] is False
        assert result["reason"]
        assert "ValueError" in result["reason"]
        assert "files" not in result


def test_package_rejects_other_project(tmp_path):
    import app.capability_export as export
    manager, pid, target, _base = _make_exported(tmp_path, "別PJ")
    other, other_pid = _make_manager(tmp_path, "別PJ-相手")
    _ = other
    result = export.read_package(manager, other_pid, target)
    assert result["ok"] is False
    assert "見つかりません" in result["reason"]
    assert "files" not in result
    missing = export.read_package(manager, pid, "0" * 32)
    assert missing["ok"] is False
    assert "files" not in missing


def test_package_tamper_missing_manifest_mismatch(tmp_path):
    import app.capability_export as export
    manager, pid, target, _base = _make_exported(tmp_path, "改ざん")
    base = _package_dir(manager, pid, target)
    # ファイルの改ざん -> 409 (中身を返さない)。
    inst = base / "instruction.json"
    inst.write_text(inst.read_text(encoding="utf-8") + " ", encoding="utf-8")
    tampered = export.read_package(manager, pid, target)
    assert tampered["ok"] is False
    assert "一致しません" in tampered["reason"]
    assert "files" not in tampered
    # 欠落 -> 409 (空や成功にしない)。
    manager2, pid2, target2, _b2 = _make_exported(tmp_path, "欠落")
    base2 = _package_dir(manager2, pid2, target2)
    (base2 / "REQUEST.md").unlink()
    missing = export.read_package(manager2, pid2, target2)
    assert missing["ok"] is False
    assert "欠けています" in missing["reason"]
    assert "files" not in missing
    # マニフェスト不一致 (実ファイルと食い違う) -> 409。
    manager3, pid3, target3, _b3 = _make_exported(tmp_path, "不一致")
    base3 = _package_dir(manager3, pid3, target3)
    manifest = json.loads((base3 / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["instruction.json"] = "0" * 64
    (base3 / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    mismatch = export.read_package(manager3, pid3, target3)
    assert mismatch["ok"] is False
    assert "一致しません" in mismatch["reason"]
    assert "files" not in mismatch


def test_package_content_hash_mismatch(tmp_path):
    import sqlite3
    import app.capability_export as export
    from app.capability_gap import gap_db_path
    manager, pid, target, _base = _make_exported(tmp_path, "内容不一致")
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE function_proposals SET content_hash=? "
                       "WHERE proposal_id=?", ("0" * 32, target))
    finally:
        db.close()
    result = export.read_package(manager, pid, target)
    assert result["ok"] is False
    assert "内容ハッシュ" in result["reason"] or "一致しません" in result["reason"]
    assert "files" not in result


def test_package_path_escape_rejected(tmp_path):
    import app.capability_export as export
    manager, pid, target, _base = _make_exported(tmp_path, "逸脱")
    base = _package_dir(manager, pid, target)
    # .. を含む提案IDは入力検証で拒否する。
    assert export.read_package(manager, pid, "../escape")["ok"] is False
    assert export.read_package(manager, pid, "/tmp/x")["ok"] is False
    # シンボリックリンクのファイルは拒否し、中身を返さない。
    link_target = tmp_path / "架空の外部.txt"
    link_target.write_text("外部", encoding="utf-8")
    victim = base / "REQUEST.md"
    saved = victim.read_bytes()
    victim.unlink()
    try:
        try:
            victim.symlink_to(link_target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege unavailable")
            raise
        result = export.read_package(manager, pid, target)
    finally:
        if victim.is_symlink() or victim.exists():
            victim.unlink(missing_ok=True)
        victim.write_bytes(saved)
    assert result["ok"] is False
    assert "シンボリックリンク" in result["reason"]
    assert "files" not in result
    # 取得APIは何も書き込まない (開発用の一時ファイルは作らない)。
    assert not link_target.is_symlink()


def test_package_api_routes(tmp_path, monkeypatch):
    import app.capability_export as export
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid, target, base_ref = _make_exported(tmp_path, "経路")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    ok = client.get(f"/api/projects/{pid}/capability-gaps/proposals/{target}/package")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert body["proposal_id"] == target
    assert body["status"] == "export_ready"
    assert body["base_ref"] == base_ref
    assert set(body["files"]) == {"instruction.json", "REQUEST.md",
                                  "manifest.json"}
    assert set(body["sha256"]) == {"instruction.json", "REQUEST.md",
                                   "manifest.json"}
    assert body["read_only"] is True
    assert body["external_sends"] == 0
    base = _package_dir(manager, pid, target)
    assert body["files"]["instruction.json"] == (
        base / "instruction.json").read_text(encoding="utf-8")
    # 別PJは404、他の状態は409、検証失敗は409 (空や成功にしない)。
    other, other_pid = _make_manager(tmp_path, "経路-相手")
    _ = other
    assert client.get(
        f"/api/projects/{other_pid}/capability-gaps/proposals/{target}/package"
    ).status_code == 404
    assert client.get(
        f"/api/projects/{pid}/capability-gaps/proposals/{'0' * 32}/package"
    ).status_code == 404
    assert client.get(
        f"/api/projects/{pid}/capability-gaps/proposals/../escape/package"
    ).status_code in (400, 404)
    (base / "REQUEST.md").unlink()
    broken = client.get(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/package")
    assert broken.status_code == 409
    assert "files" not in broken.json()
    # GETは読取専用 (呼出前後で状態・監査・ファイル不変)。
    import app.capability_gap as gap
    manager2, pid2, target2, _b2 = _make_exported(tmp_path, "経路-不変")
    monkeypatch.setattr(web, "memory", manager2.memory)
    monkeypatch.setattr(web, "orchestrator", manager2)
    base2 = _package_dir(manager2, pid2, target2)
    before = (gap.get_proposal(manager2, target2)["status"],
              len(gap.list_proposal_events(manager2, target2)),
              _file_snapshot(base2))
    listed = client.get(f"/api/projects/{pid2}/capability-gaps/proposals/{target2}/package")
    assert listed.status_code == 200
    after = (gap.get_proposal(manager2, target2)["status"],
             len(gap.list_proposal_events(manager2, target2)),
             _file_snapshot(base2))
    assert before == after
    _ = export


def test_no_jenkins_connection_and_generic_words():
    from pathlib import Path
    for name in ("app/capability_export.py", "app/capability_delivery.py",
                 "app/watchtower.py", "app/web.py"):
        lowered = Path(name).read_text(encoding="utf-8").lower()
        assert "jenkins_url" not in lowered
        assert "jenkins_job" not in lowered
        assert "jenkins_token" not in lowered
    text = Path("app/capability_export.py").read_text(encoding="utf-8")
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text
    for word in ("拡販", "販売システム", "d1-疑似"):
        assert word not in text


def test_package_rejects_symlinked_parent(tmp_path, monkeypatch):
    import app.capability_export as export
    manager, pid, target, _base = _make_exported(tmp_path, "親リンク")
    scope = export._resolve_scope_dir(manager, pid)
    parent, _final, _rel = export._package_paths(scope, target)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink",
                        lambda self: True if self == parent else original(self))
    result = export.read_package(manager, pid, target)
    assert result["ok"] is False
    assert "files" not in result


def test_base_ref_unreadable_file_is_unknown(tmp_path, monkeypatch):
    import app.capability_export as export
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "a.py").write_text("a", encoding="utf-8")
    (app_dir / "b.py").write_text("b", encoding="utf-8")
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(OSError("unreadable"))
                        if self.name == "b.py" else original(self))
    assert export.get_base_ref(tmp_path) == "unknown"
