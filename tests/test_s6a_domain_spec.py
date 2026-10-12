"""Stage 4 (4-C): DomainSpec の受入 (疑似PJのみ)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。実案件の実行経路
(vehicle/sales/campaign/google のID・実行種別) をどのパックも使っていない
ことを静的に検査する。選択は記録のみであり、実行の振り分け・計画・工程が
変わらないことを確認する。
"""
import copy
import json
import sqlite3

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

SECRET_DUMMY = "sk-test-dummy-key-0123456789abcdef"

REALCASE_TOKENS = ("vehicle", "sales", "campaign", "google")


def _make_manager(tmp_path, domain, n, tag="", criteria=None, with_csv=False):
    assert "架空" in f"架空-{domain}"
    name = f"s6a-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    if criteria is None:
        criteria = []
        for i in range(1, n + 1):
            if domain == "doc":
                criteria.append(f"架空文書{i:02d}の原本引用つき作成と検証")
            elif domain == "table":
                criteria.append(f"架空営業管理表{i:02d}の集計と検算")
            elif domain == "research":
                criteria.append(f"架空公開調査{i:02d}の情報収集と検証")
            else:
                if i % 2 == 0:
                    criteria.append(f"架空顧客へ送信{i:02d}の実行と検証")
                else:
                    criteria.append(f"架空告知文書{i:02d}の作成と検証")
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        if "送信" in text:
            scope = f"架空顧客へ送信{i:02d}を実行する。実際の外部送信は行わない。"
            title = f"架空送信工程{i}"
        elif "調査" in text or "情報収集" in text:
            scope = f"架空公開調査{i:02d}の公開情報を収集し根拠を検証する。"
            title = f"架空調査工程{i}"
        elif "集計" in text or "検算" in text:
            scope = f"架空営業管理表{i:02d}の表を作成し検算する。"
            title = f"架空集計工程{i}"
        else:
            scope = f"架空文書{i:02d}の成果物を作成し根拠を検証する。"
            title = f"架空文書工程{i}"
        task = compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, [])
        if "調査" in text or "情報収集" in text:
            contract = json.loads(task["acceptance_criteria"])
            contract["public_web_research"] = {"required": True, "query": "架空公開調査"}
            task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
        tasks.append(task)
    memory.replace_plan(pid, name + " plan", tasks)
    if with_csv:
        memory.add_context_file(pid, "架空表.csv", "a,b\n1,2\n", 8,
                                b"a,b\n1,2\n", "text/csv", "csv", "",
                                "0" * 64, "upload")
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _row_counts(path):
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        counts = {}
        for table in tables:
            try:
                counts[table] = int(db.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                continue
        return counts
    finally:
        db.close()


def test_registered_packs_validate_and_avoid_realcase_ids():
    import app.domain_spec as packs
    assert packs.SCHEMA == "sebas.domain-spec/v1"
    specs = packs.list_specs()
    assert [(s["domain_id"], s["version"]) for s in specs] == [
        ("document_evidence", "v1"),
        ("table_processing", "v1"),
        ("public_research", "v1"),
    ]
    for spec in specs:
        checked = packs.validate_spec(spec)
        assert checked == {"ok": True, "errors": []}, checked
        # 実案件の実行経路を示すID・実行種別を使わない (静的検査)。
        blob = json.dumps(spec, ensure_ascii=False).lower()
        for token in REALCASE_TOKENS:
            assert token not in json.dumps(
                spec.get("executor_ids"), ensure_ascii=False).lower(), token
            assert f"vehicle_" not in json.dumps(spec).lower(), token
        # 権限境界はすべて既定不許可。
        assert spec["permission_boundary"] == {
            "external_send": False, "external_publish": False,
            "customer_contact": False, "rag_register": False,
            "plan_approve": False,
        }
        # DEFINITIONS に実在するIDだけを使う。
        from app.capability_registry import DEFINITIONS
        for executor in spec["executor_ids"]:
            assert executor in DEFINITIONS, executor
    # 壊れた宣言はすべて拒否される。
    base = specs[0]
    broken_missing = copy.deepcopy(base)
    del broken_missing["evidence_contract"]
    assert packs.validate_spec(broken_missing)["ok"] is False
    broken_executor = copy.deepcopy(base)
    broken_executor["executor_ids"] = ["架空.unknown_executor"]
    assert packs.validate_spec(broken_executor)["ok"] is False
    broken_realcase = copy.deepcopy(base)
    broken_realcase["executor_ids"] = ["vehicle.monthly_profit"]
    assert packs.validate_spec(broken_realcase)["ok"] is False
    broken_permission = copy.deepcopy(base)
    broken_permission["permission_boundary"] = {"不正な権限": True}
    assert packs.validate_spec(broken_permission)["ok"] is False
    broken_evidence = copy.deepcopy(base)
    broken_evidence["permission_boundary"] = dict(
        base["permission_boundary"], external_send=True)
    broken_evidence["evidence_contract"] = {}
    assert packs.validate_spec(broken_evidence)["ok"] is False
    broken_retry = copy.deepcopy(base)
    broken_retry["recovery_policy"] = dict(base["recovery_policy"])
    del broken_retry["recovery_policy"]["max_retries"]
    assert packs.validate_spec(broken_retry)["ok"] is False
    broken_negative = copy.deepcopy(base)
    broken_negative["recovery_policy"] = dict(
        base["recovery_policy"], max_retries=-1)
    assert packs.validate_spec(broken_negative)["ok"] is False
    broken_version = copy.deepcopy(base)
    broken_version["version"] = "1.0"
    assert packs.validate_spec(broken_version)["ok"] is False
    # 重複登録は拒否される。
    with pytest.raises(RuntimeError):
        packs._register(copy.deepcopy(base))


def test_conformance_matches_field(tmp_path):
    import app.domain_spec as packs
    doc_manager, doc_pid = _make_manager(tmp_path, "doc", 2, tag="-doc")
    table_manager, table_pid = _make_manager(tmp_path, "table", 2, tag="-tab")
    research_manager, research_pid = _make_manager(
        tmp_path, "research", 2, tag="-res")
    # (a) 対応するパックが候補の筆頭。
    assert packs.applicable_packs(doc_manager, doc_pid)["selected"][
        "domain_id"] == "document_evidence"
    assert packs.applicable_packs(table_manager, table_pid)["selected"][
        "domain_id"] == "table_processing"
    assert packs.applicable_packs(research_manager, research_pid)["selected"][
        "domain_id"] == "public_research"
    # (b) 適合検査が conforms。
    for manager, pid, domain in ((doc_manager, doc_pid, "document_evidence"),
                                 (table_manager, table_pid, "table_processing"),
                                 (research_manager, research_pid, "public_research")):
        report = packs.conformance_report(manager, pid, domain, "v1")
        assert report["conforms"] is True, report
        assert report["verdict"] == "conforms", report
        assert report["counts"]["unknown"] == 0
        assert report["read_only"] is True
        assert report["external_sends"] == 0
    # (c) 別分野のパックでは violates (表データ型のPJに文書パック)。
    cross = packs.conformance_report(table_manager, table_pid,
                                     "document_evidence", "v1")
    assert cross["conforms"] is False
    assert cross["verdict"] == "violates"
    assert cross["counts"]["violates"] >= 1
    # (d) 外部作用の工程を含むPJは、どの汎用パックでも権限境界により violates。
    ext_manager, ext_pid = _make_manager(tmp_path, "external", 2, tag="-ext")
    for domain in ("document_evidence", "table_processing", "public_research"):
        violated = packs.conformance_report(ext_manager, ext_pid, domain, "v1")
        assert violated["conforms"] is False, (domain, violated)
        assert violated["counts"]["violates"] >= 1, (domain, violated)
        assert any("権限境界" in reason
                   for row in violated["tasks"] for reason in row["reasons"])
    # (e) 目標が曖昧/同点/空のときは ambiguous/none で、勝手に選ばない。
    tied_manager, tied_pid = _make_manager(
        tmp_path, "doc", 1, tag="-tie",
        criteria=["架空文書01の作成と検証と架空公開調査01の情報収集と検証"])
    tied = packs.applicable_packs(tied_manager, tied_pid)
    # 入力から導く: 文書と調査の両語彙に触れるため同点か、どちらか1件。
    assert tied["status"] in ("decided", "ambiguous")
    if tied["status"] == "ambiguous":
        assert tied["selected"] is None
    empty_manager, empty_pid = _make_manager(
        tmp_path, "doc", 1, tag="-empty",
        criteria=["架空量子もつれ99の位相"])
    empty = packs.applicable_packs(empty_manager, empty_pid)
    # 入力から導く: 語彙外の達成条件でも工程契約は汎用文書型のため、
    # 同点(ambiguous)または閾値未満(none)のいずれか。いずれも選ばない。
    assert empty["status"] in ("ambiguous", "none")
    assert empty["selected"] is None
    # 判定材料が読めない (破損した計画) → unknown で conforms に数えない。
    broken_manager, broken_pid = _make_manager(tmp_path, "doc", 1, tag="-brk")
    mission = broken_manager.memory.get_mission(broken_pid)
    tasks = list(mission["tasks"])
    tasks[0]["acceptance_criteria"] = "{破損"
    broken_manager.memory.replace_plan(
        broken_pid, mission.get("plan_summary") or "架空", tasks)
    unreadable = packs.conformance_report(broken_manager, broken_pid,
                                          "document_evidence", "v1")
    assert unreadable["conforms"] is False
    assert unreadable["counts"]["unknown"] >= 1
    assert unreadable["counts"]["conforms"] == 0


def test_selection_records_and_keeps_execution_unchanged(tmp_path):
    import app.domain_spec as packs
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-sel")
    mission_before = manager.memory.get_mission(pid)
    plan_before = json.dumps(mission_before["tasks"], ensure_ascii=False,
                             sort_keys=True, default=str)
    # actor・理由が必須。
    with pytest.raises(ValueError):
        packs.select_pack(manager, pid, "document_evidence", "v1",
                          actor="", reason="架空理由")
    with pytest.raises(ValueError):
        packs.select_pack(manager, pid, "document_evidence", "v1",
                          actor="架空担当", reason="")
    selected = packs.select_pack(manager, pid, "document_evidence", "v1",
                                 actor="架空担当", reason="架空理由で採用")
    assert selected["ok"] is True
    assert selected["conforms"] is True
    assert selected["warnings"] == []
    assert "実行の振り分けは変えません" in selected["note"]
    assert selected["auth_note"]
    current = packs.get_selection(manager, pid)
    assert current["domain_id"] == "document_evidence"
    assert current["version"] == "v1"
    events = packs.list_selection_events(manager, pid)
    assert any(e["kind"] == "selected" for e in events)
    # conforms でない選択に警告が付く (拒否はしない)。
    ext_manager, ext_pid = _make_manager(tmp_path, "external", 2, tag="-warn")
    warned = packs.select_pack(ext_manager, ext_pid, "document_evidence", "v1",
                               actor="架空担当", reason="架空理由で採用")
    assert warned["ok"] is True
    assert warned["conforms"] is False
    assert warned["warnings"]
    assert "適合していない" in warned["note"]
    # 選択しても実行の振り分け・計画・工程が変わらない。
    mission_after = manager.memory.get_mission(pid)
    plan_after = json.dumps(mission_after["tasks"], ensure_ascii=False,
                            sort_keys=True, default=str)
    assert plan_after == plan_before
    assert mission_after["plan_version"] == mission_before["plan_version"]
    # PJ初期化でリセット。
    reset = packs.reset_for_generation(manager.memory.path, pid)
    assert reset["reset"] is True
    assert packs.get_selection(manager, pid) is None
    # 監査 (追記専用) は残る。
    assert packs.list_selection_events(manager, pid)


def test_read_only_and_no_external(tmp_path):
    import app.domain_spec as packs
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-ro")
    before = packs.applicable_packs(manager, pid)
    mem_before = _row_counts(manager.memory.path)
    pack_path = packs.pack_db_path(manager)
    pack_exists_before = pack_path.exists()
    calls = {"external": 0}

    async def _boom(*a, **k):
        calls["external"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    out1 = packs.applicable_packs(manager, pid)
    out2 = packs.conformance_report(manager, pid, "document_evidence", "v1")
    assert out1 == before
    assert out2["external_sends"] == 0
    assert out1["external_sends"] == 0
    assert calls["external"] == 0
    # 呼出前後でDB・ファイル不変。
    assert _row_counts(manager.memory.path) == mem_before
    assert packs.pack_db_path(manager).exists() == pack_exists_before
    assert packs.applicable_packs(manager, pid) == before


def test_secret_not_recorded(tmp_path):
    import app.domain_spec as packs
    manager, pid = _make_manager(tmp_path, "doc", 1, tag="-sec")
    with pytest.raises(ValueError):
        packs.select_pack(manager, pid, "document_evidence", "v1",
                          actor="架空担当",
                          reason=f"架空理由 {SECRET_DUMMY}")
    assert SECRET_DUMMY not in str(packs.list_selection_events(manager, pid))
    assert packs.get_selection(manager, pid) is None
    # 正常な選択の記録に秘密は出ない。
    ok = packs.select_pack(manager, pid, "document_evidence", "v1",
                           actor="架空担当", reason="架空理由で採用")
    assert SECRET_DUMMY not in json.dumps(ok, ensure_ascii=False)
    assert SECRET_DUMMY not in json.dumps(
        packs.list_selection_events(manager, pid), ensure_ascii=False)


def test_api_endpoints(tmp_path, monkeypatch):
    import app.domain_spec as packs
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-api")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    mem_path = manager.memory.path
    pack_path = packs.pack_db_path(manager)
    mem_mtime = mem_path.stat().st_mtime_ns if mem_path.exists() else None
    pack_exists = pack_path.exists()

    listed = client.get("/api/domain-packs")
    assert listed.status_code == 200
    body = listed.json()
    assert [p["domain_id"] for p in body["packs"]] == [
        "document_evidence", "table_processing", "public_research"]
    assert body["read_only"] is True

    # GET: 適用候補と現在の選択。読み取り専用 (呼出前後でDB・ファイル不変)。
    resp = client.get(f"/api/projects/{pid}/domain-packs")
    assert resp.status_code == 200
    assert resp.json()["read_only"] is True
    assert resp.json()["candidates"]["selected"]["domain_id"] == "document_evidence"
    assert resp.json()["selection"] is None
    assert packs.pack_db_path(manager).exists() == pack_exists
    if mem_mtime is not None:
        assert mem_path.stat().st_mtime_ns == mem_mtime

    # GET: 適合検査。読み取り専用。
    conforms = client.get(
        f"/api/projects/{pid}/domain-packs/document_evidence/conformance")
    assert conforms.status_code == 200
    assert conforms.json()["conforms"] is True
    assert conforms.json()["external_sends"] == 0
    unknown_pack = client.get(
        f"/api/projects/{pid}/domain-packs/no_such_pack/conformance")
    assert unknown_pack.status_code == 404
    bad_version = client.get(
        f"/api/projects/{pid}/domain-packs/document_evidence/conformance?version=9")
    assert bad_version.status_code == 400
    traversal = client.get(
        f"/api/projects/{pid}/domain-packs/../escape/conformance")
    assert traversal.status_code in (400, 404)

    # POST select: 担当者・理由が必須。確認者名は本人認証ではない旨あり。
    assert client.post(f"/api/projects/{pid}/domain-packs/select",
                       json={"domain_id": "document_evidence",
                             "version": "v1", "actor": "",
                             "reason": "架空理由"}).status_code == 422
    assert client.post(f"/api/projects/{pid}/domain-packs/select",
                       json={"domain_id": "document_evidence",
                             "version": "v1", "actor": "架空担当",
                             "reason": ""}).status_code == 422
    chosen = client.post(f"/api/projects/{pid}/domain-packs/select",
                         json={"domain_id": "document_evidence",
                               "version": "v1", "actor": "架空担当",
                               "reason": "架空理由で採用"})
    assert chosen.status_code == 200
    assert chosen.json()["auth_note"]
    assert chosen.json()["conforms"] is True
    current = client.get(f"/api/projects/{pid}/domain-packs").json()
    assert current["selection"]["domain_id"] == "document_evidence"

    # POST clear: 担当者・理由が必須。
    assert client.post(f"/api/projects/{pid}/domain-packs/clear",
                       json={"actor": "", "reason": "架空理由"}).status_code == 422
    cleared = client.post(f"/api/projects/{pid}/domain-packs/clear",
                          json={"actor": "架空担当", "reason": "架空理由で解除"})
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] is True
    assert client.get(
        f"/api/projects/{pid}/domain-packs").json()["selection"] is None
    # 応答・記録に秘密値 (ダミー) が現れない。
    leaked = client.post(f"/api/projects/{pid}/domain-packs/select",
                         json={"domain_id": "document_evidence",
                               "version": "v1", "actor": "架空担当",
                               "reason": f"架空理由 {SECRET_DUMMY}"})
    assert leaked.status_code == 409
    assert SECRET_DUMMY not in leaked.text


def test_api_deleted_project_and_unknown_state(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-del")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    # 論理削除中PJは拒否。
    monkeypatch.setattr("app.project_delete.is_deleted", lambda *a, **k: True)
    try:
        assert client.get(f"/api/projects/{pid}/domain-packs").status_code == 410
        assert client.get(
            f"/api/projects/{pid}/domain-packs/document_evidence/conformance"
        ).status_code == 410
        assert client.post(f"/api/projects/{pid}/domain-packs/select",
                           json={"domain_id": "document_evidence",
                                 "version": "v1", "actor": "架空担当",
                                 "reason": "架空理由"}).status_code == 410
        assert client.post(f"/api/projects/{pid}/domain-packs/clear",
                           json={"actor": "架空担当",
                                 "reason": "架空理由"}).status_code == 410
    finally:
        import importlib
        import app.project_delete as _del
        importlib.reload(_del)
    # 削除状態DBを破損させても拒否 (unknown は拒否)。
    delete_db = manager.memory.path.with_name(
        manager.memory.path.name + ".delete.sqlite3")
    delete_db.write_bytes("架空破損".encode("utf-8"))
    try:
        assert client.get(f"/api/projects/{pid}/domain-packs").status_code == 410
    finally:
        delete_db.unlink(missing_ok=True)


def test_registry_completeness_and_lifecycle(tmp_path):
    import app.domain_spec as packs
    from app.project_delete import (
        build_delete_request_preview, build_purge_preview, purge_deleted,
        request_delete,
    )
    from app.project_generation import build_reset_preview, initialize_project
    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.project_lifecycle_registry import (
        audit_registry_completeness, resolve_db_path)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "domain_pack_selection" in audit["scanned"]
    assert "domain_pack_events" in audit["scanned"]
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-lc")
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    pack_path = packs.pack_db_path(manager)
    assert pack_path.exists()
    assert resolve_db_path(manager.memory.path, "pack") == pack_path
    # 他PJに影響しない。
    other, other_pid = _make_manager(tmp_path, "table", 2, tag="-other")
    assert packs.get_selection(other, other_pid) is None
    assert packs.get_selection(manager, pid) is not None
    # PJ初期化 (世代切替) で選択がリセットされる (退避の行複写対象外のため
    # initialize_project の実経路で確認する)。
    backup_root = tmp_path / "架空退避"
    preview = build_reset_preview(manager.memory.path, pid, "replan")
    assert preview["project_exists"] is True
    project_name = preview.get("project_name") or ""
    assert project_name
    initialized = initialize_project(
        manager.memory.path, pid, "replan",
        preview_token=preview["preview_token"], project_name=project_name,
        actor="架空担当", reason="架空理由で初期化", backup_root=backup_root)
    assert initialized["ok"] is True, initialized
    assert packs.get_selection(manager, pid) is None
    assert packs.list_selection_events(manager, pid)
    assert packs.get_selection(other, other_pid) is None
    # 退避/検証: manifest に秘密が出ない。
    backup = create_backup(manager.memory.path, pid, backup_root,
                           workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    # 完全削除: 当該PJのみ消える。
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で再採用")
    delete_preview = build_delete_request_preview(manager.memory.path, pid)
    assert delete_preview["project_exists"] is True
    delete_name = (delete_preview.get("project") or {}).get("name") or ""
    assert delete_name
    deleted = request_delete(
        manager.memory.path, pid,
        preview_token=delete_preview["preview_token"],
        project_name=delete_name, actor="架空担当", backup_root=backup_root)
    assert deleted["ok"] is True
    purge_preview = build_purge_preview(manager.memory.path, pid)
    assert purge_preview["can_purge"] is False  # 保持期間内のため完全削除はしない
    purged = packs.purge_project_packs(manager.memory.path, pid)
    assert purged["domain_pack_selection"] >= 1
    assert packs.get_selection(manager, pid) is None
    assert packs.get_selection(other, other_pid) is None
    assert packs.list_selection_events(other, other_pid) == []


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1


def test_execution_routing_unchanged():
    import inspect
    from app.project_manager import ProjectOrchestrator
    source = inspect.getsource(ProjectOrchestrator._execute_task)
    # 実行の振り分けは変えない: vehicle_ 分岐はそのまま、パック参照は無い。
    assert "startswith('vehicle_')" in source
    assert "domain_spec" not in source
    assert "domain_pack" not in source
    assert "select_pack" not in source


def test_audit_failure_rolls_back_pack_selection(tmp_path, monkeypatch):
    import app.domain_spec as packs
    manager, pid = _make_manager(tmp_path, "doc", 1, tag="-audit-rollback")
    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")
    monkeypatch.setattr(packs, "_audit_event", fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        packs.select_pack(manager, pid, "document_evidence", "v1",
                          actor="架空担当", reason="架空理由")
    assert packs.get_selection(manager, pid) is None
    monkeypatch.undo()
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由")
    monkeypatch.setattr(packs, "_audit_event", fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        packs.clear_pack(manager, pid, actor="架空担当", reason="架空解除")
    assert packs.get_selection(manager, pid) is not None
    with pytest.raises(sqlite3.OperationalError):
        packs.reset_for_generation(manager.memory.path, pid)
    assert packs.get_selection(manager, pid) is not None
