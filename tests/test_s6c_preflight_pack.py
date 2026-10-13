"""Stage 4 (4-E): 事前照合が選択された分野実行パックの宣言を参照する (疑似PJのみ)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。汎用パックだけを扱う。
実行の振り分けは変えない。提案への反映 (項目4) は実装しない (理由は完了報告)。
"""
import copy
import inspect
import json
import sqlite3

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

REALCASE_TOKENS = ("vehicle", "sales", "campaign", "google")
FIVE_STATES = ("implemented", "configured", "input_compatible",
               "validated", "permitted")
EXPECTED_PREFLIGHT_KEYS = {"schema", "project_id", "plan_signature", "criteria",
                           "gaps", "missing_capability",
                           "has_missing_capability", "gap_codes",
                           "ai_assist_used", "external_sends",
                           "external_calls", "read_only"}


# ----------------------------------------------------------------------------
# 疑似PJ (S2A流とS6A流。いずれも架空)
# ----------------------------------------------------------------------------

def _s2a_criteria(domain, n):
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


def _s2a_titles(domain, i):
    if domain == "doc":
        return f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成し根拠を検証する。"
    if domain == "table":
        return f"架空集計工程{i}", f"架空営業管理表{i:02d}の表を作成し検算する。"
    if i % 3 == 0:
        return f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。実際の外部送信は行わない。"
    return f"架空告知工程{i}", f"架空告知文書{i:02d}の成果物を作成し根拠を検証する。"


def _make_s2a_manager(tmp_path, domain, n, tag=""):
    name = f"s6c-s2a-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _s2a_criteria(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _s2a_titles(domain, i)
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


def _make_s6a_manager(tmp_path, domain, n, tag="", criteria=None):
    name = f"s6c-s6a-疑似-{domain}-{n}{tag}"
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


def _file_stamp(path):
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _five_of(op_row):
    return {key: bool(op_row.get(key)) for key in FIVE_STATES}


def _strip_pack_flags(rows):
    cleaned = copy.deepcopy(rows)
    for row in cleaned:
        for op_row in row.get("operations") or []:
            op_row.pop("pack_boundary_violation", None)
    return cleaned


# ----------------------------------------------------------------------------
# 後方互換: パック未選択では出力が従来と同一 (domain_pack キーなし)
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("domain", ["doc", "table", "external"])
def test_backward_compat_unselected_output_identical(tmp_path, domain):
    import app.capability_gap as gap
    from app.project_manager import ProjectOrchestrator
    manager, pid = _make_s2a_manager(tmp_path, domain, 3)
    if domain == "table":
        manager.memory.add_context_file(pid, "架空表.csv", "a,b\n1,2\n", 8,
                                        b"a,b\n1,2\n", "text/csv", "csv", "",
                                        "0" * 64, "upload")
        manager.table_executor = object()
        manager = ProjectOrchestrator(
            manager.memory, manager.llm, manager.static_context,
            manager.research_runner, manager.provider_statuses,
            manager.plan_review_runner, workspace=manager.workspace,
            table_executor=manager.table_executor)
    first = gap.capability_preflight(manager, pid)
    second = gap.capability_preflight(manager, pid)
    assert first == second
    assert "domain_pack" not in first
    assert set(first.keys()) == EXPECTED_PREFLIGHT_KEYS
    for row in first["criteria"]:
        for op_row in row["operations"]:
            assert "pack_boundary_violation" not in op_row
    by_criterion = {row["criterion_id"]: row for row in first["criteria"]}
    assert len(by_criterion) == 3
    if domain == "doc":
        assert first["gap_codes"] == []
        assert first["has_missing_capability"] is False
    if domain == "table":
        assert first["gap_codes"] == []
    if domain == "external":
        missing_criteria = sorted({g["criterion_id"] for g in first["missing_capability"]})
        criteria = list(by_criterion)
        assert missing_criteria == [criteria[2]]
        assert all(by_criterion[criteria[i]]["gap_code"] is None for i in (0, 1))


# ----------------------------------------------------------------------------
# 選択あり: domain_pack 付与・適合件数・resolution(実測5状態)・不足は減らない
# ----------------------------------------------------------------------------

def test_selected_pack_reports_conformance_and_measured_states(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-sel")
    before = gap.capability_preflight(manager, pid)
    assert "domain_pack" not in before
    selected = packs.select_pack(manager, pid, "document_evidence", "v1",
                                 actor="架空担当", reason="架空理由で採用")
    assert selected["ok"] is True
    after = gap.capability_preflight(manager, pid)
    assert "domain_pack" in after
    pack = after["domain_pack"]
    assert pack["selected"] == {"domain_id": "document_evidence", "version": "v1"}
    report = packs.conformance_report(manager, pid, "document_evidence", "v1")
    assert pack["conformance"]["conforms"] == report["conforms"]
    assert pack["conformance"]["verdict"] == report["verdict"]
    assert pack["conformance"]["counts"] == report["counts"]
    # resolution: 操作→パックの実行器→5状態(実測)。
    assert pack["resolution"]
    spec = packs.get_spec("document_evidence", "v1")
    declared = set(str(x) for x in spec["executor_ids"])
    before_five = {}
    for row in before["criteria"]:
        for op_row in row["operations"]:
            before_five.setdefault(str(op_row["operation_id"]), _five_of(op_row))
    for item in pack["resolution"]:
        assert item["pack_executor_id"] in declared
        assert item["pack_declared"] is True
        op = item["operation_id"]
        assert op in before_five, op
        # 5状態は選択前と同じ (宣言を理由に真にしない)。
        assert _five_of(item) == before_five[op], op
        assert item["executor_capability"] == next(
            r["executor_capability"] for row in before["criteria"]
            for r in row["operations"] if r["operation_id"] == op)
    # 事前照合の5状態自体も選択前と同じ (境界旗以外の差分なし)。
    assert _strip_pack_flags(after["criteria"]) == before["criteria"]
    # 不足は減らない (実装は不足判定を変えないため一致する)。
    assert len(after["missing_capability"]) == len(before["missing_capability"])
    assert after["missing_capability"] == before["missing_capability"]
    assert after["gap_codes"] == before["gap_codes"]
    # 選択だけで validated/permitted が真にならない。
    for row in after["criteria"]:
        for op_row in row["operations"]:
            key = (row["criterion_id"], op_row["operation_id"])
            old = next(r for crow in before["criteria"] if crow["criterion_id"] == key[0]
                       for r in crow["operations"] if r["operation_id"] == key[1])
            assert op_row["validated"] == old["validated"], key
            assert op_row["permitted"] == old["permitted"], key
    # 繰返しは決定的 (PYTHONHASHSEED 安定の材料)。
    assert gap.capability_preflight(manager, pid) == after
    order = [entry["operation_id"] for entry in gap.OPERATION_VOCABULARY]
    keys = [(order.index(i["operation_id"]) if i["operation_id"] in order else 9999,
             i["operation_id"], i["pack_executor_id"]) for i in pack["resolution"]]
    assert keys == sorted(keys)


@pytest.mark.parametrize("pack_id", ["document_evidence", "table_processing",
                                     "public_research"])
def test_missing_never_decreases_for_generic_packs(tmp_path, pack_id):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "external", 2, tag=f"-{pack_id}")
    before = gap.capability_preflight(manager, pid)
    packs.select_pack(manager, pid, pack_id, "v1",
                      actor="架空担当", reason="架空理由で採用")
    after = gap.capability_preflight(manager, pid)
    assert "domain_pack" in after
    assert after["domain_pack"]["selected"] == {"domain_id": pack_id,
                                                "version": "v1"}
    assert len(after["missing_capability"]) >= len(before["missing_capability"])
    before_ids = {(g["criterion_id"], g["operation_id"], g["gap_code"])
                  for g in before["missing_capability"]}
    after_ids = {(g["criterion_id"], g["operation_id"], g["gap_code"])
                 for g in after["missing_capability"]}
    assert before_ids <= after_ids
    assert set(before["gap_codes"]) <= set(after["gap_codes"])


# ----------------------------------------------------------------------------
# 権限境界: 許可されない外部作用に pack_boundary_violation (不足は解消しない)
# ----------------------------------------------------------------------------

def test_pack_boundary_violation_is_warning_only(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "external", 2, tag="-boundary")
    before = gap.capability_preflight(manager, pid)
    assert any(g["operation_id"] in ("external_publish", "external_send")
               for g in before["gaps"])
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    after = gap.capability_preflight(manager, pid)
    flagged = [(row["criterion_id"], op["operation_id"])
               for row in after["criteria"] for op in row["operations"]
               if op.get("pack_boundary_violation") is True]
    assert flagged
    for _cid, op_id in flagged:
        assert op_id in ("external_publish", "external_send")
    # 文書生成には付かない。
    for row in after["criteria"]:
        for op in row["operations"]:
            if op["operation_id"] == "document_generation":
                assert "pack_boundary_violation" not in op
    # 不足の判定は維持 (gap_code 不変・不足は減らない)。
    before_codes = {(r["criterion_id"],
                     next(iter([o["operation_id"] for o in r["operations"]] or [""]), ""),
                     r["gap_code"]) for r in before["criteria"]}
    after_codes = {(r["criterion_id"],
                    next(iter([o["operation_id"] for o in r["operations"]] or [""]), ""),
                    r["gap_code"]) for r in after["criteria"]}
    assert before_codes == after_codes
    assert len(after["missing_capability"]) == len(before["missing_capability"])
    assert after["domain_pack"]["conformance"]["conforms"] is False


# ----------------------------------------------------------------------------
# fail-closed: 読取失敗・適合失敗・不正宣言は unknown (不足を減らさない)
# ----------------------------------------------------------------------------

def test_fail_closed_broken_selection_db(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-broken")
    baseline = gap.capability_preflight(manager, pid)
    assert "domain_pack" not in baseline
    pack_path = packs.pack_db_path(manager)
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes("架空破損".encode("utf-8"))
    try:
        broken = gap.capability_preflight(manager, pid)
    finally:
        pack_path.unlink(missing_ok=True)
    assert broken["domain_pack"]["status"] == "unknown"
    assert broken["domain_pack"]["reason"]
    assert broken["missing_capability"] == baseline["missing_capability"]
    assert broken["gap_codes"] == baseline["gap_codes"]
    assert broken["criteria"] == baseline["criteria"]


def test_fail_closed_conformance_error(tmp_path, monkeypatch):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-conferr")
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    good = gap.capability_preflight(manager, pid)
    assert "conformance" in good["domain_pack"]

    def _boom(*args, **kwargs):
        raise RuntimeError("架空の適合検査失敗")

    monkeypatch.setattr(packs, "conformance_report", _boom)
    bad = gap.capability_preflight(manager, pid)
    assert bad["domain_pack"]["status"] == "unknown"
    assert bad["domain_pack"]["reason"]
    assert bad["missing_capability"] == good["missing_capability"]
    assert bad["gap_codes"] == good["gap_codes"]
    assert _strip_pack_flags(bad["criteria"]) == _strip_pack_flags(good["criteria"])


def test_fail_closed_invalid_declaration(tmp_path, monkeypatch):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-invalid")
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    good = gap.capability_preflight(manager, pid)
    assert "conformance" in good["domain_pack"]
    real_get = packs.get_spec

    def _invalid(domain_id, version):
        spec = real_get(domain_id, version)
        assert spec is not None
        broken = copy.deepcopy(spec)
        broken["executor_ids"] = ["架空.unknown_executor"]
        return broken

    monkeypatch.setattr(packs, "get_spec", _invalid)
    bad = gap.capability_preflight(manager, pid)
    assert bad["domain_pack"]["status"] == "unknown"
    assert bad["domain_pack"]["reason"]
    assert bad["missing_capability"] == good["missing_capability"]
    assert bad["gap_codes"] == good["gap_codes"]


def test_no_swallowing_defaults_and_read_only(tmp_path, monkeypatch):
    import app.capability_gap as gap
    import pathlib
    source = pathlib.Path("app/capability_gap.py").read_text(encoding="utf-8")
    # 4-E の追加部に通過扱いの既定値・握りつぶしが無いことを点検する。
    assert "or True" not in source
    assert "except Exception: pass" not in source
    assert "default_active" not in source.lower()
    assert "'active'" not in source
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-ro")
    mem_before = _row_counts(manager.memory.path)
    pack_path = manager.memory.path.with_name(manager.memory.path.name + ".pack.sqlite3")
    gap_path = manager.memory.path.with_name(manager.memory.path.name + ".gap.sqlite3")
    stamps_before = {str(p): _file_stamp(p) for p in
                     (manager.memory.path, pack_path, gap_path)}
    calls = {"ai": 0, "external": 0}

    def _assist(statement, hints):
        calls["ai"] += 1
        return []

    async def _boom(*args, **kwargs):
        calls["external"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    # 監査の書込があっても事前照合はそれを握りつぶさない (読取り専用のため呼ばない)。
    def _audit_boom(*args, **kwargs):
        raise RuntimeError("架空の監査失敗")

    monkeypatch.setattr(manager.memory, "add_event", _audit_boom)
    first = gap.capability_preflight(manager, pid, ai_assist=_assist)
    second = gap.capability_preflight(manager, pid, ai_assist=_assist)
    assert first == second
    assert first["external_sends"] == 0
    assert first["external_calls"] == 0
    assert calls == {"ai": calls["ai"], "external": 0}
    assert calls["ai"] >= 1
    assert gap._EXTERNAL_SENDS_COUNTER["calls"] == 0
    assert _row_counts(manager.memory.path) == mem_before
    assert {str(p): _file_stamp(p) for p in
            (manager.memory.path, pack_path, gap_path)} == stamps_before


# ----------------------------------------------------------------------------
# 分類器: conforms=false を参照できる形で渡す (既存の分類は変えない)
# ----------------------------------------------------------------------------

def test_classifier_sees_pack_nonconformance_without_changing_class(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    from app.stop_classifier import classify_stop
    manager, pid = _make_s6a_manager(tmp_path, "external", 2, tag="-cls")
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["domain_pack"]["conformance"]["conforms"] is False
    classifier_inputs = gap.preflight_to_classifier_inputs(preflight)
    assert classifier_inputs["capability_preflight"]["pack_conforms"] is False
    assert classifier_inputs["capability_preflight"]["pack_verdict"] in (
        "violates", "unknown")
    out = classify_stop(classifier_inputs)
    assert out["stop_class"] == "missing_capability"
    assert out["stop_code"] == "disposition:development"
    # パック情報が無くても分類は同じ (追加のみで現状と同一)。
    stripped = copy.deepcopy(classifier_inputs)
    stripped["capability_preflight"].pop("pack_conforms", None)
    stripped["capability_preflight"].pop("pack_verdict", None)
    assert classify_stop(stripped) == out


def test_classifier_unchanged_when_pack_conforms(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    from app.stop_classifier import classify_stop
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-cls-ok")
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["domain_pack"]["conformance"]["conforms"] is True
    classifier_inputs = gap.preflight_to_classifier_inputs(preflight)
    assert "pack_conforms" not in classifier_inputs["capability_preflight"]
    out = classify_stop(classifier_inputs)
    out2 = classify_stop({"plan_signature": classifier_inputs["plan_signature"]})
    assert out["stop_class"]
    assert out2["stop_class"]


# ----------------------------------------------------------------------------
# 提案は増殖しない (項目4は未実装のため現状の冪等を保つだけ)
# ----------------------------------------------------------------------------

def test_pack_selection_does_not_proliferate_proposals(tmp_path):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "external", 2, tag="-idem")
    preflight = gap.capability_preflight(manager, pid)
    first = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    ids_before = sorted(r["proposal_id"] for r in first["proposals"])
    hashes_before = sorted(r["content_hash"] for r in first["proposals"])
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    second = gap.generate_proposals(
        manager, pid, gap.capability_preflight(manager, pid), actor="架空担当")
    assert sorted(r["proposal_id"] for r in second["proposals"]) == ids_before
    assert sorted(r["content_hash"] for r in second["proposals"]) == hashes_before
    packs.select_pack(manager, pid, "table_processing", "v1",
                      actor="架空担当", reason="架空理由で変更")
    third = gap.generate_proposals(
        manager, pid, gap.capability_preflight(manager, pid), actor="架空担当")
    assert sorted(r["proposal_id"] for r in third["proposals"]) == ids_before


# ----------------------------------------------------------------------------
# API・画面: 既存の capability-gaps 応答に domain_pack が含まれるだけ
# ----------------------------------------------------------------------------

def test_api_capability_gaps_includes_domain_pack(tmp_path, monkeypatch):
    import app.domain_spec as packs
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-api")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    before = client.get(f"/api/projects/{pid}/capability-gaps")
    assert before.status_code == 200
    assert "domain_pack" not in before.json()["preflight"]
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    after = client.get(f"/api/projects/{pid}/capability-gaps")
    assert after.status_code == 200
    body = after.json()
    assert body["read_only"] is True
    assert body["external_sends"] == 0
    assert body["preflight"]["domain_pack"]["selected"] == {
        "domain_id": "document_evidence", "version": "v1"}
    # 既存の画面は未知のキーを無視できる (従来の材料だけで表示できる)。
    assert body["preflight"]["missing_capability"] == \
        before.json()["preflight"]["missing_capability"]


# ----------------------------------------------------------------------------
# 安全の据え置き: 新規テーブルなし・実案件不接触・振分け不変・画面不変
# ----------------------------------------------------------------------------

def test_registry_completeness_and_no_new_tables():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "function_proposals" in audit["scanned"]
    assert "domain_pack_selection" in audit["scanned"]
    import pathlib
    source = pathlib.Path("app/capability_gap.py").read_text(encoding="utf-8")
    assert source.count("CREATE TABLE") == 2  # 従来の2件のみ (新規テーブルなし)


def test_generic_packs_avoid_realcase_ids():
    import app.domain_spec as packs
    for spec in packs.list_specs():
        blob = json.dumps(spec.get("executor_ids"), ensure_ascii=False).lower()
        for token in REALCASE_TOKENS:
            assert token not in blob, token


def test_execution_routing_unchanged():
    from app.project_manager import ProjectOrchestrator
    source = inspect.getsource(ProjectOrchestrator._execute_task)
    assert "startswith('vehicle_')" in source
    assert "domain_spec" not in source
    assert "domain_pack" not in source
    assert "select_pack" not in source


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1
    assert "domain_pack" not in Path("app/static/capability_gap.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["missing_count", "wrong_count", "bool_count", "wrong_pack"])
def test_malformed_conformance_is_unknown(tmp_path, monkeypatch, fault):
    import app.capability_gap as gap
    import app.domain_spec as packs
    manager, pid = _make_s6a_manager(tmp_path, "doc", 2, tag="-malformed-" + fault)
    packs.select_pack(manager, pid, "document_evidence", "v1",
                      actor="架空担当", reason="架空理由で採用")
    baseline = gap.capability_preflight(manager, pid)
    real_report = packs.conformance_report

    def malformed(*args, **kwargs):
        report = copy.deepcopy(real_report(*args, **kwargs))
        if fault == "missing_count":
            report["counts"].pop("unknown")
        elif fault == "wrong_count":
            report["counts"]["conforms"] += 1
        elif fault == "bool_count":
            report["counts"]["conforms"] = True
        else:
            report["domain_id"] = "another_pack"
        return report

    monkeypatch.setattr(packs, "conformance_report", malformed)
    result = gap.capability_preflight(manager, pid)
    assert result["domain_pack"]["status"] == "unknown"
    assert result["missing_capability"] == baseline["missing_capability"]
    assert result["gap_codes"] == baseline["gap_codes"]
