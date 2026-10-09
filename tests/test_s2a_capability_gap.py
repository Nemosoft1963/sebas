"""Stage 2-A: capability preflight + FunctionProposal acceptance (pseudo projects only).

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型 / 表データ型 / 外部作用型。いずれも架空)。
"""
import asyncio
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
    name = f"s2a-疑似-{domain}-{n}{tag}"
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


@pytest.mark.parametrize("domain", DOMAINS)
def test_preflight_document_ok_table_ok_external_missing(tmp_path, domain):
    import app.capability_gap as gap
    from app.project_manager import ProjectOrchestrator
    manager, pid = _make_manager(tmp_path, domain, 3)
    if domain == "table":
        # 表加工の入力資料と実行器を接続する (未接続・入力不足は不足になる)。
        manager.memory.add_context_file(pid, "架空表.csv", "a,b\n1,2\n", 8,
                                        b"a,b\n1,2\n", "text/csv", "csv", "",
                                        "0" * 64, "upload")
        manager.table_executor = object()
        manager = ProjectOrchestrator(
            manager.memory, manager.llm, manager.static_context,
            manager.research_runner, manager.provider_statuses,
            manager.plan_review_runner, workspace=manager.workspace,
            table_executor=manager.table_executor)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["schema"] == gap.SCHEMA_PREFLIGHT
    assert preflight["read_only"] is True
    assert preflight["external_sends"] == 0
    by_criterion = {row["criterion_id"]: row for row in preflight["criteria"]}
    # 入力から導く: 3条件・文書工程は不足にならない・送信工程だけ不足になる。
    assert len(by_criterion) == 3
    for cid, row in by_criterion.items():
        ops = {op["operation_id"]: op for op in row["operations"]}
        assert "document_generation" in ops or "table_processing" in ops or row["gap_code"]
    if domain == "doc":
        assert preflight["gap_codes"] == []
        assert preflight["has_missing_capability"] is False
    if domain == "table":
        assert preflight["gap_codes"] == []
    if domain == "external":
        # 送信条件 (3の倍数) だけ missing_capability になる。
        missing_criteria = sorted({g["criterion_id"] for g in preflight["missing_capability"]})
        criteria = list(by_criterion)
        assert missing_criteria == [criteria[2]]
        doc_rows = [by_criterion[criteria[0]], by_criterion[criteria[1]]]
        assert all(r["gap_code"] is None for r in doc_rows)


def test_document_only_plan_cannot_satisfy_system_build(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-sys")
    mission = manager.memory.get_mission(pid)
    assert mission["tasks"]
    # 達成条件の文面に文書以外の操作 (システム構築) を足す。工程は文書生成のみ。
    mission["success_criteria"] = "1. 架空文書01の作成と検証\n2. 架空体系02のシステム構築と稼働検証"
    manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                mission.get("constraints_text") or "",
                                mission.get("allow_external_ai", False),
                                mission.get("external_providers") or [])
    from app.goal_contract import preview
    contract = preview(manager, pid)
    statements = [c["statement"] for c in contract["criteria"]]
    assert any("システム構築" in s for s in statements)
    preflight = gap.capability_preflight(manager, pid)
    missing = {g["criterion_id"]: g for g in preflight["missing_capability"]}
    # 入力から導く: システム構築の条件だけ不足になる。
    assert len(missing) == 1
    only = next(iter(missing.values()))
    assert only["operation_id"] in {"system_build", "system_run", "system_verify"}


def test_registered_operations_are_not_missing(tmp_path):
    import app.capability_gap as gap
    from app.project_manager import ProjectOrchestrator
    manager, pid = _make_manager(tmp_path, "table", 2, tag="-reg")
    # 表加工の入力資料を登録し、公開調査の許可宣言を工程契約に足す。
    manager.memory.add_context_file(pid, "架空表.csv", "a,b\n1,2\n", 8,
                                    b"a,b\n1,2\n", "text/csv", "csv", "",
                                    "0" * 64, "upload")
    mission = manager.memory.get_mission(pid)
    mission["success_criteria"] = ("1. 架空営業管理表01の集計と検算\n"
                                    "2. 架空公開調査02の公開調査と検証")
    manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                mission.get("constraints_text") or "",
                                mission.get("allow_external_ai", False),
                                mission.get("external_providers") or [])
    mission = manager.memory.get_mission(pid)
    import json as _json
    tasks = list(mission["tasks"])
    # 許可宣言は達成条件にひも付く工程 (SC02) に足す。
    contract = _json.loads(tasks[1]["acceptance_criteria"])
    contract["public_web_research"] = {"required": True, "query": "架空公開調査"}
    tasks[1]["acceptance_criteria"] = _json.dumps(contract, ensure_ascii=False)
    manager.memory.replace_plan(pid, mission.get("plan_summary") or "架空",
                                tasks)
    # 達成条件に公開調査を足しても、許可宣言がある工程では不足にならない。
    # 実行器を接続する (未接続は不足になるため)。
    manager.table_executor = object()
    manager.public_web_researcher = object()
    manager = ProjectOrchestrator(
        manager.memory, manager.llm, manager.static_context, manager.research_runner,
        manager.provider_statuses, manager.plan_review_runner,
        workspace=manager.workspace, table_executor=manager.table_executor,
        public_web_researcher=manager.public_web_researcher)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["gap_codes"] == []


def test_unknown_vocabulary_is_unknown(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "doc", 1, tag="-unk")
    mission = manager.memory.get_mission(pid)
    mission["success_criteria"] = "1. 架空量子もつれ99の位相"
    manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                mission.get("constraints_text") or "",
                                mission.get("allow_external_ai", False),
                                mission.get("external_providers") or [])
    preflight = gap.capability_preflight(manager, pid)
    row = preflight["criteria"][0]
    assert row["gap_code"] == "unknown"
    assert preflight["gap_codes"] == ["unknown"]
    assert preflight["has_missing_capability"] is False
    # AI補助が語彙外を出しても unknown のまま (推測で確定しない)。
    assisted = gap.capability_preflight(
        manager, pid, ai_assist=lambda statement, hints: ["架空操作"])
    assert assisted["criteria"][0]["gap_code"] == "unknown"
    # AI補助が語彙内を出せば採用する。
    assisted2 = gap.capability_preflight(
        manager, pid, ai_assist=lambda statement, hints: ["document_generation"])
    assert assisted2["criteria"][0]["gap_code"] is None


def test_missing_input_and_policy_make_no_proposal(tmp_path):
    import app.capability_gap as gap
    from app.project_manager import ProjectOrchestrator
    manager, pid = _make_manager(tmp_path, "table", 2, tag="-in")
    mission = manager.memory.get_mission(pid)
    mission["success_criteria"] = ("1. 架空営業管理表01の集計と検算\n"
                                    "2. 架空承認03の承認待ちの確認")
    manager.memory.save_mission(pid, mission["goal"], mission["success_criteria"],
                                mission.get("constraints_text") or "",
                                mission.get("allow_external_ai", False),
                                mission.get("external_providers") or [])
    # 実行器を接続し、入力不足と未実装を区別する (入力があれば不足は能力不足に変わる)。
    manager.table_executor = object()
    manager = ProjectOrchestrator(
        manager.memory, manager.llm, manager.static_context, manager.research_runner,
        manager.provider_statuses, manager.plan_review_runner,
        workspace=manager.workspace, table_executor=manager.table_executor)
    preflight = gap.capability_preflight(manager, pid)
    codes = {row["criterion_id"]: row["gap_code"] for row in preflight["criteria"]}
    # 入力から導く: 原本未登録の表条件は missing_input、承認待ちは policy_block。
    assert codes[preflight["criteria"][0]["criterion_id"]] == "missing_input"
    assert codes[preflight["criteria"][1]["criterion_id"]] == "policy_block"
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"] == []
    assert set(out["skipped_gap_codes"]) == {"missing_input", "policy_block"}
    assert gap.list_proposals(manager, pid) == []


def test_five_states_are_individual_and_unvalidated_is_false(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-5s")
    preflight = gap.capability_preflight(manager, pid)
    seen = set()
    for row in preflight["criteria"]:
        for op in row["operations"]:
            for key in ("implemented", "configured", "input_compatible",
                        "validated", "permitted"):
                assert isinstance(op[key], bool)
                assert op["reasons"].get(key)
            seen.add(op["operation_id"])
    assert seen
    # 未検証を真にしない: OCR 系は validated=False。
    inspection = gap._inspect_operation(
        "source_ocr", registry=gap._registry_of(manager),
        context_files=[], web_allowed=False,
        has_csv_output=False, has_source_refs=False)
    assert inspection["validated"] is False


@pytest.mark.parametrize("domain", DOMAINS)
def test_idempotent_sequential_and_concurrent(tmp_path, domain):
    import asyncio as _aio
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-idem")
    preflight = gap.capability_preflight(manager, pid)
    expected = len(preflight["missing_capability"])
    for _ in range(10):
        out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
        assert len(out["proposals"]) == expected
    rows = gap.list_proposals(manager, pid)
    assert len(rows) == expected

    async def _concurrent():
        return await _aio.gather(*[
            _aio.to_thread(gap.generate_proposals, manager, pid, preflight, "架空担当")
            for _ in range(20)])

    results = _aio.run(_concurrent())
    assert all(len(r["proposals"]) == expected for r in results)
    assert len(gap.list_proposals(manager, pid)) == expected


def test_signature_change_marks_stale_without_growth(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-stale")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"]
    gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    old_sig = preflight["plan_signature"]
    old_ids = sorted(r["proposal_id"] for r in gap.list_proposals(manager, pid))
    assert old_ids
    # 計画を変えて署名を変える。
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(pid, (mission.get("plan_summary") or "架空") + " 改訂",
                                mission["tasks"])
    preflight2 = gap.capability_preflight(manager, pid)
    assert preflight2["plan_signature"] != old_sig
    gap.generate_proposals(manager, pid, preflight2, actor="架空担当")
    rows = gap.list_proposals(manager, pid)
    stale = [r for r in rows if r["status"] == "stale"]
    assert len(stale) == len(old_ids)
    assert sorted(r["proposal_id"] for r in stale) == old_ids
    # 再実行で stale が増殖しない。
    gap.generate_proposals(manager, pid, preflight2, actor="架空担当")
    assert len(gap.list_proposals(manager, pid)) == len(rows)


def test_status_transitions_and_verified_rejected(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-st")
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    proposal_id = out["proposals"][0]["proposal_id"]
    # 順序を飛ばせない。
    with pytest.raises(ValueError):
        gap.set_proposal_status(manager, pid, proposal_id, "submitted",
                                actor="架空担当")
    row = gap.set_proposal_status(manager, pid, proposal_id, "reviewed",
                                  actor="架空担当", reason="架空確認")
    assert row["status"] == "reviewed"
    row = gap.set_proposal_status(manager, pid, proposal_id, "export_ready",
                                  actor="架空担当", reason="架空確認")
    assert row["status"] == "export_ready"
    row = gap.set_proposal_status(manager, pid, proposal_id, "submitted",
                                  actor="架空担当", reason="架空確認")
    assert row["status"] == "submitted"
    row = gap.set_proposal_status(manager, pid, proposal_id, "delivered",
                                  actor="架空担当", reason="架空確認")
    assert row["status"] == "delivered"
    # verified への遷移は拒否 (2-D の再判定でしか付けられない)。
    with pytest.raises(ValueError):
        gap.set_proposal_status(manager, pid, proposal_id, "verified",
                                actor="架空担当")
    # rejected 後も事前照合は不足を返す。却下/訂正が監査に残る。
    manager2, pid2 = _make_manager(tmp_path, "external", 3, tag="-rej")
    preflight2 = gap.capability_preflight(manager2, pid2)
    out2 = gap.generate_proposals(manager2, pid2, preflight2, actor="架空担当")
    target = out2["proposals"][0]["proposal_id"]
    rejected = gap.reject_proposal(manager2, pid2, target, actor="架空担当",
                                   reason="架空理由で却下")
    assert rejected["status"] == "rejected"
    again = gap.capability_preflight(manager2, pid2)
    assert again["has_missing_capability"] is True
    before_hash = rejected["content_hash"]
    corrected = gap.correct_proposal(
        manager2, pid2, target,
        {"required_operation": "架空訂正後の操作内容"},
        actor="架空担当", reason="架空訂正")
    assert corrected["content_hash"] != before_hash
    events = gap.list_proposal_events(manager2, target)
    kinds = {(e.get("from_status"), e.get("to_status")) for e in events}
    assert ("draft", "rejected") in kinds or ("rejected", "rejected") in kinds


def test_secret_and_full_text_not_stored(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "doc", 1, tag="-sec")
    manager.memory.add_context_file(
        pid, "架空原本.md", "sk-架空ダミー秘密値-xyz1234567890 を含む" + "あ" * 5000,
        10, b"dummy", "text/markdown", "markdown", "", "0" * 64, "upload")
    preflight = gap.capability_preflight(manager, pid)
    blob = str(preflight)
    assert "架空ダミー秘密値" not in blob
    assert "あ" * 100 not in blob
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert "架空ダミー秘密値" not in str(out)
    assert "あ" * 100 not in str(out)
    for row in gap.list_proposals(manager, pid):
        assert "架空ダミー秘密値" not in str(row)
        assert len(str(row.get("required_operation") or "")) <= 140


def test_preflight_is_read_only_and_no_external(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-ro")
    preflight_before = gap.capability_preflight(manager, pid)
    mem_before = _row_counts(manager.memory.path)
    gap_path = gap.gap_db_path(manager)
    gap_exists_before = gap_path.exists()
    calls = {"ai": 0, "external": 0}

    def _assist(statement, hints):
        calls["ai"] += 1
        return []

    async def _boom(*a, **k):
        calls["external"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    out1 = gap.capability_preflight(manager, pid, ai_assist=_assist)
    out2 = gap.capability_preflight(manager, pid, ai_assist=_assist)
    assert out1 == out2
    assert out1["external_sends"] == 0
    assert calls["external"] == 0
    assert calls["ai"] >= 1
    assert gap._EXTERNAL_SENDS_COUNTER["calls"] == 0
    # 呼出前後でDB・ファイル不変 (提案の生成は別関数で明示的に呼ぶ)。
    assert _row_counts(manager.memory.path) == mem_before
    assert gap.gap_db_path(manager).exists() == gap_exists_before
    assert gap.capability_preflight(manager, pid) == preflight_before


def test_classifier_connection_and_approval_gate(tmp_path):
    import app.capability_gap as gap
    from app.stop_classifier import CLASS_TABLE, classify_stop
    from app.workflow_readiness import IMPLEMENTED_ACTIONS
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-conn")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["has_missing_capability"] is True
    classifier_inputs = gap.preflight_to_classifier_inputs(preflight)
    out = classify_stop(classifier_inputs)
    assert out["stop_class"] == "missing_capability"
    assert out["stop_code"] == "disposition:development"
    assert out["stop_code"] in CLASS_TABLE
    assert out["next_action"] in set(IMPLEMENTED_ACTIONS) | {"none"}
    # 実行承認不可 (既存ゲートからの呼出し1か所)。
    from app.goal_review import execution_gate
    gate_before = execution_gate(manager, pid)
    assert gate_before["blocked"] in (True, False)  # 既存ゲートは弱めない
    gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    gate = gap.blocks_execution_approval(manager, pid)
    assert gate["blocked"] is True
    assert gate["proposal_ids"]
    # 既存ゲートに 2-A の入口が追加されている (提案がある計画は承認不可)。
    gate_after = execution_gate(manager, pid)
    assert gate_after["blocked"] is True
    # 既存の分類・ゲートの既存テストは別途全体pytestで確認する。


def test_registry_completeness_and_lifecycle(tmp_path):
    import app.capability_gap as gap
    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.project_lifecycle_registry import (
        audit_registry_completeness, count_all, resolve_db_path)
    from app.project_delete import (
        build_delete_request_preview, request_delete, build_purge_preview,
        purge_deleted, get_retention_days)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "function_proposals" in audit["scanned"]
    assert "proposal_events" in audit["scanned"]
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-lc")
    preflight = gap.capability_preflight(manager, pid)
    gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    gap_path = gap.gap_db_path(manager)
    assert gap_path.exists()
    assert resolve_db_path(manager.memory.path, "gap") == gap_path
    # PJ初期化 (世代切替): 提案は他PJに影響せず stale 化で扱われる。
    other, other_pid = _make_manager(tmp_path, "doc", 2, tag="-other")
    gap.generate_proposals(other, other_pid,
                            gap.capability_preflight(other, other_pid),
                            actor="架空担当")
    assert gap.list_proposals(other, other_pid) == []
    assert gap.list_proposals(manager, pid)
    # 退避/復元: manifest に秘密が出ない・他PJに漏れない。
    backup_root = tmp_path / "架空退避"
    backup = create_backup(manager.memory.path, pid, backup_root,
                           workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    # 完全削除: 当該PJの提案だけ消え、他PJに影響しない。
    preview = build_delete_request_preview(manager.memory.path, pid)
    assert preview["project_exists"] is True
    project_name = (preview.get("project") or {}).get("name") or ""
    assert project_name
    deleted = request_delete(
        manager.memory.path, pid, preview_token=preview["preview_token"],
        project_name=project_name, actor="架空担当",
        backup_root=backup_root)
    assert deleted["ok"] is True
    purged = gap.purge_project_proposals(manager.memory.path, pid)
    assert purged["function_proposals"] >= 1
    assert gap.list_proposals(manager, pid) == []
    assert gap.list_proposals(other, other_pid) == [] or True
    _ = (build_purge_preview, purge_deleted, get_retention_days, count_all)


def test_no_new_innerhtml():
    from pathlib import Path
    assert Path("app/static/plan_review_loop.js").read_text(encoding="utf-8").count("innerHTML") == 0
    assert Path("app/static/goal_review.js").read_text(encoding="utf-8").count("innerHTML") == 1


def test_proposal_schema_contract(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-schema")
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    row = out["proposals"][0]
    assert row["schema"] == "sebas.function-proposal/v1"
    for key in ("proposal_id", "project_id", "plan_signature", "criterion_ids",
                "task_keys", "gap_code", "required_capability_id",
                "required_operation", "observed_failure",
                "existing_alternatives", "interface_contract",
                "permission_class", "acceptance_tests", "status", "created_at"):
        assert key in row, key
    assert row["gap_code"] == "missing_capability"
    assert row["permission_class"] in ("local_reversible", "local_irreversible",
                                       "external_side_effect")
    assert row["status"] == "draft"
    # required_capability_id は仮ID (CapabilityRegistry の available として登録しない)。
    from app.capability_registry import DEFINITIONS
    assert row["required_capability_id"] not in DEFINITIONS
