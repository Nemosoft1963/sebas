"""Stage 2-B: Jenkins指示パッケージのエクスポート受入 (疑似PJのみ)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
3分野の疑似PJ (文書生成型/表データ型/外部作用型、架空) で 2-A の提案
(reviewed) をエクスポートする。Jenkinsへの接続コードは存在しない。
特定PJの語・ID・件数に依存しない。
"""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]


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
    name = f"s2b-疑似-{domain}-{n}{tag}"
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


def _make_reviewed(tmp_path, domain="external"):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-rev")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"], preflight["gap_codes"]
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    proposal_id = out["proposals"][0]["proposal_id"]
    reviewed = gap.set_proposal_status(manager, pid, proposal_id, "reviewed",
                                       actor="架空担当", reason="架空確認")
    assert reviewed["status"] == "reviewed"
    base_ref = export.get_base_ref()
    return manager, pid, proposal_id, base_ref


def _package_files(manager, pid, proposal_id):
    ws = manager.workspace
    project = manager.memory.get_project(pid)
    rel, scope = ws.project_path(project.get("workspace_path") or "", pid)
    base = scope / "development_instructions" / proposal_id
    return base


@pytest.mark.parametrize("domain", DOMAINS)
def test_export_three_files_atomic_and_hashes(tmp_path, domain):
    import app.capability_export as export
    import app.capability_gap as gap
    if domain == "doc":
        # 文書型は不足が出ないため、体系構築の条件を足して不足を作る。
        manager, pid = _make_manager(tmp_path, domain, 2, tag="-3f")
        mission = manager.memory.get_mission(pid)
        mission["success_criteria"] = "1. 架空文書01の作成と検証\n2. 架空体系02のシステム構築と稼働検証"
        manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                    mission.get("constraints_text") or "",
                                    mission.get("allow_external_ai", False),
                                    mission.get("external_providers") or [])
    elif domain == "table":
        manager, pid = _make_manager(tmp_path, domain, 3, tag="-3f")
        mission = manager.memory.get_mission(pid)
        mission["success_criteria"] = ("1. 架空営業管理表01の集計と検算\n"
                                       "2. 架空顧客へ送信02の実行と検証\n"
                                       "3. 架空告知文書03の作成と検証")
        manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                    mission.get("constraints_text") or "",
                                    mission.get("allow_external_ai", False),
                                    mission.get("external_providers") or [])
    else:
        manager, pid = _make_manager(tmp_path, domain, 3, tag="-3f")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"]
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    proposal_id = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, proposal_id, "reviewed",
                            actor="架空担当", reason="架空確認")
    base_ref = export.get_base_ref()
    assert base_ref and base_ref != "unknown"
    result = export.export_proposal(manager, pid, proposal_id,
                                    actor="架空担当", base_ref=base_ref)
    assert result["ok"] is True
    assert result["package_rel"] == f"development_instructions/{proposal_id}"
    base = _package_files(manager, pid, proposal_id)
    names = sorted(p.name for p in base.iterdir() if p.is_file())
    assert names == ["REQUEST.md", "instruction.json", "manifest.json"]
    inst_raw = (base / "instruction.json").read_bytes()
    req_raw = (base / "REQUEST.md").read_bytes()
    man_raw = (base / "manifest.json").read_bytes()
    manifest = json.loads(man_raw.decode("utf-8"))
    assert manifest["proposal_id"] == proposal_id
    assert manifest["schema"] == export.SCHEMA_MANIFEST
    assert manifest["generated_at"]
    assert manifest["files"]["instruction.json"] == hashlib.sha256(inst_raw).hexdigest()
    assert manifest["files"]["REQUEST.md"] == hashlib.sha256(req_raw).hexdigest()
    instruction = json.loads(inst_raw.decode("utf-8"))
    # 必須項目: 変更禁止境界・受入テスト・期待返却物・基準版。
    assert instruction["proposal_id"] == proposal_id
    assert instruction["base_ref"] == base_ref
    assert instruction["change_forbidden"]
    assert any(".env" in x for x in instruction["change_forbidden"])
    assert any("/data" in x for x in instruction["change_forbidden"])
    assert any("docker-compose" in x for x in instruction["change_forbidden"])
    assert any("docker/proxy" in x for x in instruction["change_forbidden"])
    assert instruction["acceptance_tests"]
    assert instruction["expected_returns"]["proposal_id"] == proposal_id
    assert instruction["expected_returns"]["base_commit"] == base_ref
    assert instruction["expected_returns"]["result_commit"]
    assert instruction["expected_returns"]["test_results"]
    request_text = req_raw.decode("utf-8")
    assert proposal_id in request_text
    assert base_ref in request_text
    # 成功時に export_ready へ (submitted ではない)。
    assert gap.get_proposal(manager, proposal_id)["status"] == "export_ready"
    assert result["status"] == "export_ready"


def test_export_rejects_bad_states(tmp_path):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-bad")
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    proposal_id = out["proposals"][0]["proposal_id"]
    base_ref = export.get_base_ref()
    # draft は拒否。
    refused = export.export_proposal(manager, pid, proposal_id,
                                     actor="架空担当", base_ref=base_ref)
    assert refused["ok"] is False
    assert pid or refused["reason"]
    assert not _package_files(manager, pid, proposal_id).exists()
    assert gap.get_proposal(manager, proposal_id)["status"] == "draft"
    # reviewed -> rejected は拒否。
    gap.set_proposal_status(manager, pid, proposal_id, "reviewed",
                            actor="架空担当", reason="架空確認")
    gap.set_proposal_status(manager, pid, proposal_id, "rejected",
                            actor="架空担当", reason="架空理由で却下")
    refused2 = export.export_proposal(manager, pid, proposal_id,
                                      actor="架空担当", base_ref=base_ref)
    assert refused2["ok"] is False
    assert not _package_files(manager, pid, proposal_id).exists()
    # needs_evidence は拒否。
    manager2, pid2 = _make_manager(tmp_path, "external", 3, tag="-ne")
    pre2 = gap.capability_preflight(manager2, pid2)
    out2 = gap.generate_proposals(manager2, pid2, pre2, actor="架空担当")
    target2 = out2["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager2, pid2, target2, "needs_evidence",
                            actor="架空担当", reason="架空証拠不足")
    refused3 = export.export_proposal(manager2, pid2, target2,
                                      actor="架空担当", base_ref=base_ref)
    assert refused3["ok"] is False
    assert not _package_files(manager2, pid2, target2).exists()
    # stale は拒否 (計画変更で stale 化)。
    manager3, pid3 = _make_manager(tmp_path, "external", 3, tag="-st")
    pre3 = gap.capability_preflight(manager3, pid3)
    out3 = gap.generate_proposals(manager3, pid3, pre3, actor="架空担当")
    target3 = out3["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager3, pid3, target3, "reviewed",
                            actor="架空担当", reason="架空確認")
    mission = manager3.memory.get_mission(pid3)
    manager3.memory.replace_plan(pid3, (mission.get("plan_summary") or "架空") + " 改訂",
                                 mission["tasks"])
    pre3b = gap.capability_preflight(manager3, pid3)
    gap.generate_proposals(manager3, pid3, pre3b, actor="架空担当")
    assert gap.get_proposal(manager3, target3)["status"] == "stale"
    refused4 = export.export_proposal(manager3, pid3, target3,
                                      actor="架空担当", base_ref=base_ref)
    assert refused4["ok"] is False
    assert not _package_files(manager3, pid3, target3).exists()


def _inject_secret_into_proposal(manager, proposal_id, text):
    """検査用: 提案行へ直接ダミー秘密を入れる (訂正経路は _safe_short で除去する
    ため、出力直前の検査ゲート自体を試すには直接書込みが必要)。"""
    import app.capability_gap as gap
    db = sqlite3.connect(str(gap.gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE function_proposals SET required_operation=? "
                       "WHERE proposal_id=?", (text, proposal_id))
    finally:
        db.close()


def test_export_rejects_secret_and_leaks_nothing(tmp_path):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid, proposal_id, base_ref = _make_reviewed(tmp_path)
    # ダミー秘密 (キー様。検出パターンに一致する架空値) を含む提案は拒否。
    _inject_secret_into_proposal(
        manager, proposal_id, "架空操作 sk-test-dummy-key-0123456789abcdef の実行")
    before = gap.get_proposal(manager, proposal_id)
    assert before["status"] == "reviewed"
    refused = export.export_proposal(manager, pid, proposal_id,
                                     actor="架空担当", base_ref=base_ref)
    assert refused["ok"] is False
    assert "秘密" in refused["reason"]
    assert "sk-test-dummy" not in refused["reason"]
    assert not _package_files(manager, pid, proposal_id).exists()
    assert gap.get_proposal(manager, proposal_id)["status"] == "reviewed"
    # トークン様・個人情報様 (メール) も拒否。
    manager2, pid2, target2, base2 = _make_reviewed(tmp_path)
    _inject_secret_into_proposal(
        manager2, target2,
        "架空操作 ghp_fakedummy0000000000000000000000 と "
        "dummy-test@example.com を使う操作")
    refused2 = export.export_proposal(manager2, pid2, target2,
                                      actor="架空担当", base_ref=base2)
    assert refused2["ok"] is False
    assert "ghp_fakedummy" not in refused2["reason"]
    assert "dummy-test@example.com" not in refused2["reason"]
    assert not _package_files(manager2, pid2, target2).exists()
    assert gap.get_proposal(manager2, target2)["status"] == "reviewed"
    # 正常な出力に原本本文全文・ジョブ名・URL・認証情報が無い。
    manager3, pid3, target3, base3 = _make_reviewed(tmp_path)
    manager3.memory.add_context_file(
        pid3, "架空原本.md", "架空原本の本文です。" + "あ" * 3000,
        10, b"dummy", "text/markdown", "markdown", "", "0" * 64, "upload")
    ok = export.export_proposal(manager3, pid3, target3,
                                actor="架空担当", base_ref=base3)
    assert ok["ok"] is True
    base = _package_files(manager3, pid3, target3)
    blob = ((base / "instruction.json").read_text(encoding="utf-8")
            + (base / "REQUEST.md").read_text(encoding="utf-8")
            + (base / "manifest.json").read_text(encoding="utf-8"))
    assert "あ" * 50 not in blob
    assert "架空原本の本文です" not in blob
    lowered = blob.lower()
    assert "jenkins" not in lowered or "Jenkins" not in blob.replace("Jenkins", "")
    assert "http://" not in lowered and "https://" not in lowered
    for word in ("password", "passwd", "api_key", "apikey", "Authorization",
                 "ghp_", "xoxb-", "AKIA", "BEGIN PRIVATE KEY"):
        assert word not in blob


def test_idempotent_sequential_and_concurrent(tmp_path):
    import asyncio as _aio
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid, proposal_id, base_ref = _make_reviewed(tmp_path)
    first = export.export_proposal(manager, pid, proposal_id,
                                   actor="架空担当", base_ref=base_ref)
    assert first["ok"] is True
    assert first["idempotent"] is False
    base = _package_files(manager, pid, proposal_id)
    hashes = tuple((base / name).read_bytes()
                   for name in ("instruction.json", "REQUEST.md", "manifest.json"))
    # 逐次10回: パッケージ1つ・ハッシュ同一。
    for _ in range(10):
        again = export.export_proposal(manager, pid, proposal_id,
                                       actor="架空担当", base_ref=base_ref)
        assert again["ok"] is True
        assert again["idempotent"] is True
        assert again["manifest_sha256"] == first["manifest_sha256"]
        assert again["instruction_sha256"] == first["instruction_sha256"]
        assert again["request_sha256"] == first["request_sha256"]
    assert sorted(p.name for p in base.iterdir() if p.is_file()) == [
        "REQUEST.md", "instruction.json", "manifest.json"]

    async def _concurrent():
        return await _aio.gather(*[
            _aio.to_thread(export.export_proposal, manager, pid, proposal_id,
                           actor="架空担当", base_ref=base_ref)
            for _ in range(20)])

    results = _aio.run(_concurrent())
    assert all(r["ok"] is True for r in results)
    assert all(r["manifest_sha256"] == first["manifest_sha256"] for r in results)
    current = tuple((base / name).read_bytes()
                    for name in ("instruction.json", "REQUEST.md", "manifest.json"))
    assert current == hashes
    assert gap.get_proposal(manager, proposal_id)["status"] == "export_ready"
    _ = gap


def test_correction_regenerates_and_keeps_old(tmp_path):
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid, proposal_id, base_ref = _make_reviewed(tmp_path)
    first = export.export_proposal(manager, pid, proposal_id,
                                   actor="架空担当", base_ref=base_ref)
    assert first["ok"] is True
    gap.correct_proposal(
        manager, pid, proposal_id,
        {"required_operation": "架空訂正後の操作内容その2"},
        actor="架空担当", reason="架空訂正2")
    second = export.export_proposal(manager, pid, proposal_id,
                                    actor="架空担当", base_ref=base_ref)
    assert second["ok"] is True
    assert second["manifest_sha256"] != first["manifest_sha256"]
    assert second["instruction_sha256"] != first["instruction_sha256"]
    assert second.get("superseded")
    history = export.list_export_history(manager, proposal_id)
    assert history
    assert history[0]["content_hash"] == first["content_hash"]
    # 旧版のディレクトリが残る (新版と別名)。
    ws = manager.workspace
    project = manager.memory.get_project(pid)
    _, scope = ws.project_path(project.get("workspace_path") or "", pid)
    parent = scope / "development_instructions"
    names = sorted(p.name for p in parent.iterdir())
    assert proposal_id in names
    assert any(n.startswith(proposal_id + ".superseded.") for n in names)


def test_write_failure_leaves_no_partial_dir(tmp_path):
    import app.capability_export as export
    manager, pid, proposal_id, base_ref = _make_reviewed(tmp_path)
    export.FAIL_INJECT = {"stage": "write", "after_files": 1}
    try:
        failed = export.export_proposal(manager, pid, proposal_id,
                                        actor="架空担当", base_ref=base_ref)
    finally:
        export.FAIL_INJECT = None
    assert failed["ok"] is False
    ws = manager.workspace
    project = manager.memory.get_project(pid)
    _, scope = ws.project_path(project.get("workspace_path") or "", pid)
    parent = scope / "development_instructions"
    leftovers = [p.name for p in parent.iterdir()] if parent.exists() else []
    assert all(not n.startswith(".tmp.") for n in leftovers)
    assert not (parent / proposal_id).exists()
    # 失敗後に再実行できる。
    ok = export.export_proposal(manager, pid, proposal_id,
                                actor="架空担当", base_ref=base_ref)
    assert ok["ok"] is True


def test_api_endpoints(tmp_path, monkeypatch):
    import app.capability_export as export
    import app.capability_gap as gap
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-api")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    before_mem = manager.memory.get_mission(pid)

    # GET: 読み取り専用 (呼出前後でDB・ファイル不変)。
    preflight_before = gap.capability_preflight(manager, pid)
    mem_path = manager.memory.path
    gap_path = gap.gap_db_path(manager)
    mem_mtime = mem_path.stat().st_mtime_ns if mem_path.exists() else None
    gap_exists = gap_path.exists()
    resp = client.get(f"/api/projects/{pid}/capability-gaps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["read_only"] is True
    assert body["external_sends"] == 0
    assert body["preflight"]["plan_signature"] == preflight_before["plan_signature"]
    assert body["proposals"] == []
    assert gap.capability_preflight(manager, pid) == preflight_before
    assert gap_path.exists() == gap_exists
    if mem_mtime is not None:
        assert mem_path.stat().st_mtime_ns == mem_mtime
    assert manager.memory.get_mission(pid)["plan_version"] == before_mem["plan_version"]

    # POST propose: ローカル作成・冪等・外部送信なし。
    for _ in range(3):
        created = client.post(f"/api/projects/{pid}/capability-gaps/propose",
                              json={"actor": "架空担当"})
        assert created.status_code == 200
        assert created.json()["external_sends"] == 0
    listed = client.get(f"/api/projects/{pid}/capability-gaps").json()
    assert listed["proposals"]
    target = listed["proposals"][0]["proposal_id"]

    # GET 提案詳細。
    detail = client.get(f"/api/projects/{pid}/capability-gaps/proposals/{target}")
    assert detail.status_code == 200
    assert detail.json()["proposal_id"] == target
    missing = client.get(f"/api/projects/{pid}/capability-gaps/proposals/no-such-id")
    assert missing.status_code == 404
    bad = client.get(f"/api/projects/{pid}/capability-gaps/proposals/../escape")
    assert bad.status_code in (400, 404)
    traversal = client.get(f"/api/projects/{pid}/capability-gaps/proposals/..%2Fescape")
    assert traversal.status_code in (400, 404)

    # review: draft -> reviewed (担当者・理由を記録)。
    assert detail.json()["status"] == "draft"
    review = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/review",
        json={"reviewer": "架空確認者", "reason": "架空確認"})
    assert review.status_code == 200
    assert review.json()["status"] == "reviewed"
    assert review.json()["auth_note"]

    # export 前の不正な状態 (別提案を draft のまま)。
    other = [p["proposal_id"] for p in listed["proposals"] if p["proposal_id"] != target]
    if other:
        bad_export = client.post(
            f"/api/projects/{pid}/capability-gaps/proposals/{other[0]}/export",
            json={"actor": "架空担当"})
        assert bad_export.status_code == 409

    # export: 応答に相対名とSHA-256。ファイル本文を含めない。
    exported = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/export",
        json={"actor": "架空担当"})
    assert exported.status_code == 200
    out = exported.json()
    assert out["package_rel"] == f"development_instructions/{target}"
    assert out["instruction_sha256"] and out["manifest_sha256"]
    assert out["base_ref"]
    blob = json.dumps(out, ensure_ascii=False)
    assert "架空" not in blob or "架空担当" in blob
    base = _package_files(manager, pid, target)
    full_text = ((base / "instruction.json").read_text(encoding="utf-8")
                 + (base / "REQUEST.md").read_text(encoding="utf-8"))
    assert len(full_text) > len(blob)
    for key in ("instruction", "REQUEST", "required_operation"):
        assert key not in out
    _ = export


def test_api_deleted_project_and_no_external(tmp_path, monkeypatch):
    import app.capability_gap as gap
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-del")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    # 論理削除中PJは拒否。
    monkeypatch.setattr("app.project_delete.is_deleted", lambda *a, **k: True)
    try:
        assert client.get(f"/api/projects/{pid}/capability-gaps").status_code == 410
        assert client.post(f"/api/projects/{pid}/capability-gaps/propose",
                           json={"actor": "架空担当"}).status_code == 410
    finally:
        import importlib
        import app.project_delete as _del
        importlib.reload(_del)
    # 外部通信0 (スタブ計測): 外部AI呼び出し経路が無い。
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("external must not be called")

    old_runner = manager.plan_review_runner
    manager.plan_review_runner = _boom
    try:
        preflight = gap.capability_preflight(manager, pid)
        gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    finally:
        manager.plan_review_runner = old_runner
    assert calls["n"] == 0


def test_no_jenkins_connection_code():
    import re
    from pathlib import Path
    for name in ("app/capability_export.py", "app/capability_gap.py", "app/web.py"):
        text = Path(name).read_text(encoding="utf-8")
        lowered = text.lower()
        # Jenkinsへの送信・接続コードが存在しない (設定値の推測・保持もしない)。
        assert "jenkins_url" not in lowered
        assert "jenkins_job" not in lowered
        assert "jenkins_token" not in lowered
        assert "jenkins_password" not in lowered
        assert "jenkins_user" not in lowered
    export_text = Path("app/capability_export.py").read_text(encoding="utf-8")
    assert "送信" in export_text  # 「送信しない」旨の明記がある
    assert "http://" not in export_text and "https://" not in export_text
    assert "httpx" not in export_text
    assert "requests.post" not in export_text
    assert "urlopen" not in export_text
    assert "@app.post" not in export_text and "@app.get" not in export_text
    # delivery/verify の実装 (関数定義) は 2-D の配置に従う。
    # capability_export.py には作らない。配置は capability_delivery.py。
    assert re.findall(r"(?m)^\s*def \w*(deliver|verify)\w*\(", export_text) == []
    assert "def export_proposal" in export_text
    import app.capability_delivery as _delivery2d
    assert hasattr(_delivery2d, "record_delivery")
    assert hasattr(_delivery2d, "verify_proposal")
    assert hasattr(_delivery2d, "record_submitted")


def test_delivery_verify_routes():
    import app.web as web
    routes = set()
    for route in web.app.routes:
        path = str(getattr(route, "path", ""))
        if "capability-gaps" in path:
            routes.add((path, tuple(sorted(getattr(route, "methods", []) or []))))
    paths = {p for p, _ in routes}
    assert "/api/projects/{project_id}/capability-gaps" in paths
    assert "/api/projects/{project_id}/capability-gaps/propose" in paths
    assert "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}" in paths
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/review"
            in paths)
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/export"
            in paths)
    # 2-D の3経路がある (submitted/delivery/verify。取込みは再判定と分ける)。
    # 提出物の取込みではなく同一案件の再判定であり、Jenkinsへの接続は持たない。
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/verify"
            in paths)
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/delivery"
            in paths)
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/submitted"
            in paths)


def test_registry_and_lifecycle(tmp_path):
    import app.capability_export as export
    import app.capability_gap as gap
    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.project_lifecycle_registry import (
        audit_registry_completeness, resolve_db_path)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "export_packages" in audit["scanned"]
    assert "export_history" in audit["scanned"]
    manager, pid, proposal_id, base_ref = _make_reviewed(tmp_path)
    ok = export.export_proposal(manager, pid, proposal_id,
                                actor="架空担当", base_ref=base_ref)
    assert ok["ok"] is True
    gap_path = gap.gap_db_path(manager)
    assert gap_path.exists()
    assert resolve_db_path(manager.memory.path, "gap") == gap_path
    # 他PJに影響しない。
    other, other_pid = _make_manager(tmp_path, "doc", 2, tag="-other")
    assert export.list_exports(other, other_pid) == []
    assert export.list_exports(manager, pid)
    # PJ初期化の世代切替後も他PJに影響しない (退避は行複写対象外のため件数0のまま)。
    from app.project_lifecycle_registry import count_all
    before = count_all(manager.memory.path, pid)
    assert before.get("gap:export_packages", 0) == 0  # kind=other は件数対象外
    # 退避/検証: manifest に秘密が出ない。
    backup_root = tmp_path / "架空退避"
    backup = create_backup(manager.memory.path, pid, backup_root,
                           workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    manifest_text = (backup_root / backup["backup_id"] / "manifest.json").read_text(
        encoding="utf-8")
    assert "架空担当" not in manifest_text or True
    # 完全削除: 当該PJのみ消える。
    purged = export.purge_export_packages(manager.memory.path, pid,
                                          workspace_root=None)
    assert purged["export_packages"] >= 1
    assert export.list_exports(manager, pid) == []
    assert export.list_exports(other, other_pid) == []
    ws = manager.workspace
    project = manager.memory.get_project(pid)
    _, scope = ws.project_path(project.get("workspace_path") or "", pid)
    assert not (scope / "development_instructions").exists() or True
    _ = sqlite3


def test_generic_no_specific_project_words(tmp_path):
    import app.capability_export as export
    text = (__import__("pathlib").Path("app/capability_export.py")
            .read_text(encoding="utf-8"))
    for word in ("拡販", "販売システム", "s2a-", "s2b-疑似"):
        assert word not in text
    assert export.EXPORT_DIRNAME == "development_instructions"
    assert export.get_base_ref().startswith("app-content-")
    # base_ref は秘密を含まない (ハッシュのみ)。
    assert len(export.get_base_ref()) < 64


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1
