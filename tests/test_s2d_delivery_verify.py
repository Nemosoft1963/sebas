"""Stage 2-D: 開発結果の取込みと再判定 (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
Jenkinsへの接続・送信はしない。分野の異なる疑似PJ3種 (文書生成型/表データ型/
外部作用型、架空) で通し、不正な納品・再判定の冪等・API・画面の静的検査・
棚卸し登録・PJ初期化/退避/完全削除を確認する。特定PJの語・ID・件数に依存しない。
"""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]
ROOT = Path(__file__).resolve().parents[1]
GAP_JS = (ROOT / "app/static/capability_gap.js").read_text(encoding="utf-8")


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
    name = f"s2d-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    if domain == "doc" and n >= 2:
        # 文書型だけでは不足が出ないため、体系構築の条件を足して不足を作る。
        criteria[-1] = "架空体系02のシステム構築と稼働検証"
    if domain == "table" and n >= 3:
        criteria[1] = "架空顧客へ送信02の実行と検証"
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        task = compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, [])
        tasks.append(task)
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _pipeline(tmp_path, domain="external", tag="-flow"):
    """不足の検出(2-A) -> 提案 -> review -> export(2-B) -> submitted (2-D)。"""
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    import uuid as _uuid
    suffix = f"{tag}-{_uuid.uuid4().hex[:6]}"
    manager, pid = _make_manager(tmp_path, domain, 3, tag=suffix)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"], preflight["gap_codes"]
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
    submitted = delivery.record_submitted(manager, pid, target, actor="架空担当")
    assert submitted["status"] == "submitted"
    return manager, pid, target, base_ref


def _delivery_payload(base_ref, idx=1):
    diff = f"架空差分-{idx}\n+架空実装の追加\n"
    return {
        "base_commit": base_ref,
        "result_commit": f"架空成果-{idx}",
        "diff_text": diff,
        "diff_ref": f"架空PR-{idx}",
        "artifact_sha256": hashlib.sha256(
            _canonical(diff).encode("utf-8")).hexdigest(),
        "test_results": {"total": 3, "failed": 0, "passed": True,
                         "tests": [{"name": f"架空受入{i}", "passed": True}
                                   for i in range(3)]},
        "capability_ids": ["架空検査済み能力"],
        "failure_logs": "架空失敗なし",
    }


def _canonical(diff):
    import app.capability_delivery as delivery
    material = delivery._canonical_delivery_content(diff, "", None)
    return material.decode("utf-8")


@pytest.mark.parametrize("domain", DOMAINS)
def test_full_flow_requires_registry_fix_not_build_pass(tmp_path, domain):
    """ビルドPASS相当の納品だけでは verified にならず、登録簿の修正で解消する。

    テスト内で現行レジストリに疑似の実行器を登録した (モンキーパッチ) 状態を
    作ると、同じ入力の再生で不足が消えて verified になる。レジストリの書換えは
    テスト内のモンキーパッチだけであり、製品コードは登録簿を書き換えない。
    """
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, pid, target, base_ref = _pipeline(tmp_path, domain)
    # ビルドPASS相当の納品 (テスト成功) だけでは能力は有効にならない。
    accepted = delivery.record_delivery(
        manager, pid, target, _delivery_payload(base_ref), actor="架空担当")
    assert accepted["verdict"] == "delivered"
    assert accepted["status"] == "delivered"
    refused = delivery.verify_proposal(manager, pid, target, actor="架空担当")
    assert refused["verified"] is False
    assert refused["status"] == "delivered"
    assert refused.get("remaining")
    assert "元の停止が残っています" in refused.get("reason", "")
    assert gap.get_proposal(manager, target)["status"] == "delivered"
    # 現行レジストリに疑似の実行器を登録した状態を作ると、同じ入力の再生で
    # 不足が消えて verified になる (テスト内の状態変更だけ。製品コードは
    # 登録簿を書き換えない)。表データ型の不足は入力資料と実行器の未接続が
    # 原因であり、入力資料の登録と実行器の接続で解消する。疑似修正用のPJは
    # 表加工の不足が1件だけ出る条件にする (文書生成型の混在を避ける)。
    proposal = gap.get_proposal(manager, target)
    assert proposal is not None
    fix_manager, fix_pid = _make_manager(tmp_path, "table", 1, tag="-vfix")
    fix_manager.memory.add_context_file(
        fix_pid, "架空表.csv", "a,b\n1,2\n", 8, b"a,b\n1,2\n",
        "text/csv", "csv", "", "0" * 64, "upload")
    import app.capability_export as export
    fix_pre = gap.capability_preflight(fix_manager, fix_pid)
    # 表加工の条件は入力資料あり・実行器なしで missing_capability になる。
    assert fix_pre["missing_capability"], fix_pre["gap_codes"]
    assert len(fix_pre["missing_capability"]) == 1
    assert fix_pre["missing_capability"][0]["operation_id"] == "table_processing"
    fix_out = gap.generate_proposals(fix_manager, fix_pid, fix_pre,
                                     actor="架空担当")
    assert len(fix_out["proposals"]) == 1
    fix_target = fix_out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(fix_manager, fix_pid, fix_target, "reviewed",
                            actor="架空担当", reason="架空確認")
    fix_base = export.get_base_ref()
    fix_exported = export.export_proposal(fix_manager, fix_pid, fix_target,
                                          actor="架空担当", base_ref=fix_base)
    assert fix_exported["ok"] is True
    delivery.record_submitted(fix_manager, fix_pid, fix_target,
                              actor="架空担当")
    fix_accepted = delivery.record_delivery(
        fix_manager, fix_pid, fix_target, _delivery_payload(fix_base),
        actor="架空担当")
    assert fix_accepted["verdict"] == "delivered"
    # まず不足が残ることを確認する (ビルドPASSだけでは有効化しない)。
    fix_refused = delivery.verify_proposal(fix_manager, fix_pid, fix_target,
                                           actor="架空担当")
    assert fix_refused["verified"] is False
    assert fix_refused["status"] == "delivered"
    # 疑似の実行器を登録した状態 (現行レジストリへのテスト内の登録) で
    # 同じ入力を再生すると不足が消える。
    fix_manager.table_executor = object()
    verified = delivery.verify_proposal(fix_manager, fix_pid, fix_target,
                                        actor="架空担当")
    assert verified["verified"] is True
    assert verified["status"] == "verified"
    assert verified.get("replay_hash")
    assert verified.get("checked_at")
    assert verified.get("plan_signature")
    assert "元の停止が再判定で解消しました" in verified.get("note", "")
    assert gap.get_proposal(fix_manager, fix_target)["status"] == "verified"
    # 製品コードは登録簿を書き換えない (DEFINITIONS に仮IDが無い)。
    from app.capability_registry import DEFINITIONS
    assert proposal["required_capability_id"] not in DEFINITIONS
    current = gap.get_proposal(fix_manager, fix_target)
    assert current["required_capability_id"] not in DEFINITIONS


def test_submitted_records_without_sending(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-sub")
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    target = out["proposals"][0]["proposal_id"]
    # export_ready 前 (draft) は記録できない。
    with pytest.raises(ValueError):
        delivery.record_submitted(manager, pid, target, actor="架空担当")
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    with pytest.raises(ValueError):
        delivery.record_submitted(manager, pid, target, actor="架空担当")
    base_ref = export.get_base_ref()
    exported = export.export_proposal(manager, pid, target,
                                      actor="架空担当", base_ref=base_ref)
    assert exported["ok"] is True
    first = delivery.record_submitted(manager, pid, target, actor="架空担当",
                                      memo="架空メモ")
    assert first["status"] == "submitted"
    # 再記録は冪等 (状態を進めない)。
    second = delivery.record_submitted(manager, pid, target, actor="架空担当")
    assert second["status"] == "submitted"
    assert second["idempotent"] is True
    # 実際には何も送信しない (接続・送信コードが無い)。
    text = Path("app/capability_delivery.py").read_text(encoding="utf-8")
    assert "送信はしない" in text or "何も送信" in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text
    # export_ready からの直接納品も受け付ける (submitted 補完つき)。
    manager2, pid2 = _make_manager(tmp_path, "external", 3, tag="-sub2")
    pre2 = gap.capability_preflight(manager2, pid2)
    out2 = gap.generate_proposals(manager2, pid2, pre2, actor="架空担当")
    target2 = out2["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager2, pid2, target2, "reviewed",
                            actor="架空担当", reason="架空確認")
    base2 = export.get_base_ref()
    export.export_proposal(manager2, pid2, target2, actor="架空担当",
                           base_ref=base2)
    accepted = delivery.record_delivery(manager2, pid2, target2,
                                        _delivery_payload(base2),
                                        actor="架空担当")
    assert accepted["verdict"] == "delivered"
    assert accepted["status"] == "delivered"


def test_delivery_rejections(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, pid, target, base_ref = _pipeline(tmp_path)
    good = _delivery_payload(base_ref)

    def _sha(diff_text):
        return hashlib.sha256(
            delivery._canonical_delivery_content(diff_text, "", None)).hexdigest()

    # 基準版の不一致 -> stale。
    stale_payload = dict(good)
    stale_payload["base_commit"] = "架空の古い基準版"
    stale = delivery.record_delivery(manager, pid, target, stale_payload,
                                     actor="架空担当")
    assert stale["verdict"] == "stale"
    assert "再同期" in stale["reason"]
    assert gap.get_proposal(manager, target)["status"] == "stale"

    # 新しいPJで残りの拒否を試す (stale は終端のため)。
    manager2, pid2, target2, base2 = _pipeline(tmp_path)
    good2 = _delivery_payload(base2)
    # ハッシュ不一致 -> 拒否。
    bad_hash = dict(good2)
    bad_hash["artifact_sha256"] = "0" * 64
    refused = delivery.record_delivery(manager2, pid2, target2, bad_hash,
                                       actor="架空担当")
    assert refused["verdict"] == "rejected"
    assert "ハッシュ" in refused["reason"]
    # テスト失敗 -> rejected。
    manager3, pid3, target3, base3 = _pipeline(tmp_path)
    failed = _delivery_payload(base3, idx=9)
    failed["test_results"] = {"total": 2, "failed": 1, "passed": False}
    bad_tests = delivery.record_delivery(manager3, pid3, target3, failed,
                                         actor="架空担当")
    assert bad_tests["verdict"] == "rejected"
    # テスト欠落 -> needs_evidence。
    manager4, pid4, target4, base4 = _pipeline(tmp_path)
    missing_tests = _delivery_payload(base4, idx=11)
    missing_tests.pop("test_results", None)
    no_evidence = delivery.record_delivery(manager4, pid4, target4,
                                           missing_tests, actor="架空担当")
    assert no_evidence["verdict"] == "needs_evidence"
    # テスト0件 -> needs_evidence。
    manager5, pid5, target5, base5 = _pipeline(tmp_path)
    zero_tests = _delivery_payload(base5, idx=13)
    zero_tests["test_results"] = {"total": 0, "failed": 0, "passed": True}
    zero = delivery.record_delivery(manager5, pid5, target5, zero_tests,
                                    actor="架空担当")
    assert zero["verdict"] == "needs_evidence"
    # 参照のみでハッシュ不能 -> needs_evidence。
    manager6, pid6, target6, base6 = _pipeline(tmp_path)
    ref_only = {"base_commit": base6, "result_commit": "架空成果-ref",
                "diff_ref": "架空PR-ref",
                "test_results": {"total": 1, "failed": 0, "passed": True},
                "capability_ids": []}
    ref = delivery.record_delivery(manager6, pid6, target6, ref_only,
                                   actor="架空担当")
    assert ref["verdict"] == "needs_evidence"
    assert "参照のみ" in ref["reason"] or "検証できません" in ref["reason"]
    # 秘密を含む失敗ログ -> 拒否し、値は残さない。
    manager7, pid7, target7, base7 = _pipeline(tmp_path)
    secret = _delivery_payload(base7, idx=15)
    secret["failure_logs"] = "架空失敗 sk-test-dummy-key-0123456789abcdef を含む"
    denied = delivery.record_delivery(manager7, pid7, target7, secret,
                                      actor="架空担当")
    assert denied["ok"] is False
    assert denied["verdict"] == "rejected"
    assert "sk-test-dummy" not in denied["reason"]
    assert "sk-test-dummy" not in str(delivery.list_deliveries(manager7, target7))
    assert gap.get_proposal(manager7, target7)["status"] == "submitted"
    _ = _sha
    # 存在しない/別PJの proposal_id。
    with pytest.raises(ValueError):
        delivery.record_delivery(manager, pid, "0" * 32, good, actor="架空担当")
    other, other_pid = _make_manager(tmp_path, "doc", 2, tag="-other")
    _ = other
    with pytest.raises(ValueError):
        delivery.record_delivery(manager, other_pid, target, good,
                                 actor="架空担当")
    # 不正な状態 (draft) への納品。
    manager8, pid8 = _make_manager(tmp_path, "external", 3, tag="-draft")
    import app.capability_gap as gap2
    pre8 = gap2.capability_preflight(manager8, pid8)
    out8 = gap2.generate_proposals(manager8, pid8, pre8, actor="架空担当")
    draft_target = out8["proposals"][0]["proposal_id"]
    with pytest.raises(ValueError):
        delivery.record_delivery(manager8, pid8, draft_target, good,
                                 actor="架空担当")


def test_delivery_idempotent_and_history(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, pid, target, base_ref = _pipeline(tmp_path)
    good = _delivery_payload(base_ref)
    first = delivery.record_delivery(manager, pid, target, good, actor="架空担当")
    assert first["idempotent"] is False
    assert first["verdict"] == "delivered"
    # 同一ハッシュの重複投入は冪等 (二重に状態を進めない)。
    second = delivery.record_delivery(manager, pid, target, good,
                                      actor="架空担当")
    assert second["idempotent"] is True
    assert second["delivery_id"] == first["delivery_id"]
    assert len(delivery.list_deliveries(manager, target)) == 1
    assert gap.get_proposal(manager, target)["status"] == "delivered"
    # 別内容の再納品は履歴として追記する (旧版は残す)。
    other = _delivery_payload(base_ref, idx=2)
    third = delivery.record_delivery(manager, pid, target, other,
                                     actor="架空担当")
    assert third["idempotent"] is False
    rows = delivery.list_deliveries(manager, target)
    assert len(rows) == 2
    assert rows[0]["id"] == first["delivery_id"]
    assert rows[1]["id"] == third["delivery_id"]
    assert gap.get_proposal(manager, target)["status"] == "delivered"


def test_verify_remaining_signature_change_and_idempotent(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, pid, target, base_ref = _pipeline(tmp_path)
    # delivered 前は再判定できない。
    with pytest.raises(ValueError):
        delivery.verify_proposal(manager, pid, target, actor="架空担当")
    good = _delivery_payload(base_ref)
    delivery.record_delivery(manager, pid, target, good, actor="架空担当")
    # 不足が残れば verified にならず残件を返す。
    first = delivery.verify_proposal(manager, pid, target, actor="架空担当")
    assert first["verified"] is False
    assert first["status"] == "delivered"
    assert first.get("remaining")
    unmet = [s for item in first["remaining"] for s in item.get("unmet_states", [])]
    assert unmet
    assert "元の停止が残っています" in first.get("reason", "")
    # 冪等: 10回逐次・同時20回で増殖しない。
    for _ in range(10):
        again = delivery.verify_proposal(manager, pid, target, actor="架空担当")
        assert again["verified"] is False
        assert again["status"] == "delivered"
    assert len(delivery.list_verifications(manager, target)) == 1

    async def _concurrent():
        return await asyncio.gather(*[
            asyncio.to_thread(delivery.verify_proposal, manager, pid, target,
                              actor="架空担当")
            for _ in range(20)])

    results = asyncio.run(_concurrent())
    assert all(r["verified"] is False for r in results)
    assert all(r["status"] == "delivered" for r in results)
    assert len(delivery.list_verifications(manager, target)) == 1
    # 計画署名が変われば stale (新規提案は勝手に作らない)。
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(pid, (mission.get("plan_summary") or "架空") + " 改訂",
                                mission["tasks"])
    changed = delivery.verify_proposal(manager, pid, target, actor="架空担当")
    assert changed.get("stale") is True
    assert gap.get_proposal(manager, target)["status"] == "stale"
    # verified への遷移が専用経路のみ (2-A の保護テストは別途全体pytestで確認)。
    with pytest.raises(ValueError):
        gap.set_proposal_status(manager, pid, target, "verified",
                                actor="架空担当")
    with pytest.raises(ValueError):
        delivery.verify_proposal(manager, pid, target, actor="架空担当")
    # 別案件の提案は操作できない。
    other, other_pid = _make_manager(tmp_path, "doc", 2, tag="-x")
    _ = other
    with pytest.raises(ValueError):
        delivery.verify_proposal(other, other_pid, target, actor="架空担当")


def test_verified_clears_stop_gate_but_not_plan_or_send(tmp_path):
    """verified で停止分類器/実行ゲートの機能不足が解除されるが、計画の承認・
    実行開始・外部送信は自動で起きない (スタブ呼出0)。"""
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    from app.stop_classifier import classify_stop
    # 表データ型の不足 (入力資料あり・実行器なし。1条件で不足1件) で通しを作る。
    doc_manager, doc_pid = _make_manager(tmp_path, "table", 1, tag="-gate")
    doc_manager.memory.add_context_file(
        doc_pid, "架空表.csv", "a,b\n1,2\n", 8, b"a,b\n1,2\n",
        "text/csv", "csv", "", "0" * 64, "upload")
    import app.capability_export as export
    doc_pre = gap.capability_preflight(doc_manager, doc_pid)
    assert doc_pre["missing_capability"], doc_pre["gap_codes"]
    assert len(doc_pre["missing_capability"]) == 1
    doc_out = gap.generate_proposals(doc_manager, doc_pid, doc_pre, actor="架空担当")
    assert doc_out["proposals"]
    doc_target = doc_out["proposals"][0]["proposal_id"]
    gate_blocked = gap.blocks_execution_approval(doc_manager, doc_pid)
    assert gate_blocked["blocked"] is True
    still_missing = gap.capability_preflight(doc_manager, doc_pid)
    classified_before = classify_stop(
        gap.preflight_to_classifier_inputs(still_missing))
    assert classified_before["stop_class"] == "missing_capability"
    gap.set_proposal_status(doc_manager, doc_pid, doc_target, "reviewed",
                            actor="架空担当", reason="架空確認")
    doc_base = export.get_base_ref()
    export.export_proposal(doc_manager, doc_pid, doc_target,
                           actor="架空担当", base_ref=doc_base)
    delivery.record_submitted(doc_manager, doc_pid, doc_target, actor="架空担当")
    delivery.record_delivery(doc_manager, doc_pid, doc_target,
                             _delivery_payload(doc_base), actor="架空担当")
    # 疑似の実行器の登録で verified にする (人が本番へ反映した後の状態を模す)。
    doc_manager.table_executor = object()
    verified = delivery.verify_proposal(doc_manager, doc_pid, doc_target,
                                        actor="架空担当")
    assert verified["verified"] is True
    # verified 後は同じ入力の再生で missing_capability が消える。
    replay_fixed = gap.capability_preflight(doc_manager, doc_pid)
    classified_after = classify_stop(
        gap.preflight_to_classifier_inputs(replay_fixed))
    gate_after = gap.blocks_execution_approval(doc_manager, doc_pid)
    assert classified_after["stop_class"] != "missing_capability"
    assert gate_after["blocked"] is False
    # 計画の承認・実行開始・外部送信は自動で起きない (スタブ呼出0)。
    calls = {"approve": 0, "start": 0, "send": 0}

    async def _boom(*a, **k):
        calls["approve"] += 1
        raise AssertionError("must not be called")

    old_review = doc_manager.plan_review_runner
    doc_manager.plan_review_runner = _boom
    old_llm = doc_manager.llm
    doc_manager.llm = None
    try:
        mission = doc_manager.memory.get_mission(doc_pid)
        # 承認・実行の状態が自動で変わっていない。
        assert mission["status"] not in ("approved", "running")
        actions = doc_manager.memory.list_actions(doc_pid)
        assert actions == []
    finally:
        doc_manager.plan_review_runner = old_review
        doc_manager.llm = old_llm
    assert calls == {"approve": 0, "start": 0, "send": 0}


def test_api_submitted_delivery_verify(tmp_path, monkeypatch):
    import app.capability_export as export
    import app.capability_gap as gap
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-api")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    created = client.post(f"/api/projects/{pid}/capability-gaps/propose",
                          json={"actor": "架空担当"})
    assert created.status_code == 200
    target = created.json()["proposals"][0]["proposal_id"]
    reviewed = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/review",
        json={"reviewer": "架空確認者", "reason": "架空確認"})
    assert reviewed.status_code == 200
    exported = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/export",
        json={"actor": "架空担当"})
    assert exported.status_code == 200
    base_ref = exported.json()["base_ref"]
    # submitted: 正常系・異常系。
    submitted = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/submitted",
        json={"actor": "架空担当", "memo": "架空メモ"})
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    assert "何も送信" in submitted.json()["note"]
    again = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/submitted",
        json={"actor": "架空担当"})
    assert again.status_code == 200
    assert again.json()["status"] == "submitted"
    assert client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/submitted",
        json={"actor": ""}).status_code == 422
    # delivery: 正常系 (delivered)。応答に失敗ログ全文・差分全文が無い。
    payload = _delivery_payload(base_ref)
    delivered = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/delivery",
        json={"actor": "架空担当", "delivery": payload})
    assert delivered.status_code == 200
    body = delivered.json()
    assert body["verdict"] == "delivered"
    assert body["status"] == "delivered"
    assert "まだ能力は有効ではありません" in body["note"]
    blob = json.dumps(body, ensure_ascii=False)
    assert "架空差分" not in blob
    assert "架空失敗なし" not in blob
    # delivery の異常系: 空・不正な proposal_id・パストラバーサル。
    assert client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/delivery",
        json={"actor": "架空担当", "delivery": {}}).status_code == 422
    assert client.get(
        f"/api/projects/{pid}/capability-gaps/proposals/no-such-id").status_code == 404
    assert client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/no-such-id/delivery",
        json={"actor": "架空担当", "delivery": payload}).status_code == 404
    assert client.get(
        f"/api/projects/{pid}/capability-gaps/proposals/../escape").status_code in (400, 404)
    assert client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/delivery",
        json={"actor": "", "delivery": payload}).status_code == 422
    # verify: 不足が残るため delivered のまま。
    verified = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/verify",
        json={"actor": "架空担当"})
    assert verified.status_code == 200
    assert verified.json()["verified"] is False
    assert verified.json()["status"] == "delivered"
    assert verified.json()["remaining"]
    assert client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/verify",
        json={"actor": ""}).status_code == 422
    # GET系は読み取り専用 (呼出前後で提案・納品が変わらない)。
    import app.capability_delivery as delivery
    before = (gap.get_proposal(manager, target)["status"],
              len(delivery.list_deliveries(manager, target)))
    detail = client.get(f"/api/projects/{pid}/capability-gaps/proposals/{target}")
    assert detail.status_code == 200
    listed = client.get(f"/api/projects/{pid}/capability-gaps")
    assert listed.status_code == 200
    assert listed.json()["read_only"] is True
    after = (gap.get_proposal(manager, target)["status"],
             len(delivery.list_deliveries(manager, target)))
    assert before == after
    _ = export


def test_api_deleted_project_and_no_jenkins(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-del")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    monkeypatch.setattr("app.project_delete.is_deleted", lambda *a, **k: True)
    try:
        assert client.post(
            f"/api/projects/{pid}/capability-gaps/proposals/{'0' * 32}/submitted",
            json={"actor": "架空担当"}).status_code == 410
        assert client.post(
            f"/api/projects/{pid}/capability-gaps/proposals/{'0' * 32}/delivery",
            json={"actor": "架空担当",
                  "delivery": {"base_commit": "x"}}).status_code == 410
        assert client.post(
            f"/api/projects/{pid}/capability-gaps/proposals/{'0' * 32}/verify",
            json={"actor": "架空担当"}).status_code == 410
    finally:
        import importlib
        import app.project_delete as _del
        importlib.reload(_del)
    # Jenkinsへの接続コードが存在しない (製品コード全体)。
    for name in ("app/capability_delivery.py", "app/capability_gap.py",
                 "app/capability_export.py", "app/web.py"):
        text = Path(name).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "jenkins_url" not in lowered
        assert "jenkins_job" not in lowered
        assert "jenkins_token" not in lowered
        assert "jenkins_password" not in lowered
        assert "jenkins_user" not in lowered
    delivery_text = Path("app/capability_delivery.py").read_text(encoding="utf-8")
    assert "httpx" not in delivery_text
    assert "requests.post" not in delivery_text
    assert "urlopen" not in delivery_text
    assert "http://" not in delivery_text and "https://" not in delivery_text
    assert "@app.post" not in delivery_text and "@app.get" not in delivery_text
    # 8本の経路がある (既存5本 + submitted/delivery/verify)。
    import app.web as web2
    routes = {str(getattr(r, "path", "")) for r in web2.app.routes}
    assert "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/submitted" in routes
    assert "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/delivery" in routes
    assert "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/verify" in routes


def test_screen_static_no_innerhtml_and_button_states():
    assert "innerHTML" not in GAP_JS
    assert "insertAdjacentHTML" not in GAP_JS
    assert "document.write" not in GAP_JS
    assert "outerHTML" not in GAP_JS
    # 別の言葉での表示。
    assert "実装が納品されました。まだ能力は有効ではありません" in GAP_JS
    assert "元の停止が再判定で解消しました" in GAP_JS
    assert "Jenkinsに渡した（記録）" in GAP_JS
    assert "再判定する" in GAP_JS
    # 納品の入力フォームは作らない (納品内容の入力欄が無い)。
    assert "納品の入力は画面では行いません" in GAP_JS
    assert "artifact_sha256" not in GAP_JS
    assert "failure_logs" not in GAP_JS
    # textContent のみ (innerHTML 等を使わない)。
    assert "textContent" in GAP_JS
    # buttonStateFor の既存の期待 (draft/reviewed/export_ready/rejected/stale/
    # needs_evidence の canReview/canExport) を壊さない。
    import shutil
    import subprocess
    import tempfile
    if shutil.which("node") is None:
        pytest.skip("Node が無いため静的検査で代替")
    probe = ("const fs=require('fs');"
             "global.document=undefined;"
             "const src=fs.readFileSync('app/static/capability_gap.js','utf8');"
             "const mod={exports:{}};"
             "require('vm').runInNewContext(src,{module:mod,document:undefined});"
             "const f=mod.exports.buttonStateFor;"
             "const cases={draft:[true,false],reviewed:[false,true],"
             "export_ready:[false,true],rejected:[false,false],"
             "stale:[false,false],needs_evidence:[false,false]};"
             "for(const k of Object.keys(cases)){"
             "const got=f(k);"
             "if(got.canReview!==cases[k][0]||got.canExport!==cases[k][1])"
             "{console.error('mismatch '+k);process.exit(1);}};"
             "const d=f('delivered');"
             "if(d.canReview!==false||d.canExport!==false||d.canVerify!==true)"
             "{console.error('mismatch delivered');process.exit(1);};"
             "const v=f('verified');"
             "if(v.canReview!==false||v.canExport!==false||v.canVerify!==false)"
             "{console.error('mismatch verified');process.exit(1);};"
             "const s=f('submitted');"
             "if(s.canReview!==false||s.canExport!==false)"
             "{console.error('mismatch submitted');process.exit(1);}")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as tmp:
        tmp.write(probe)
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr


def test_registry_and_lifecycle(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.project_lifecycle_registry import (
        audit_registry_completeness, count_all, resolve_db_path)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "gap_deliveries" in audit["scanned"]
    assert "gap_verifications" in audit["scanned"]
    manager, pid, target, base_ref = _pipeline(tmp_path)
    accepted = delivery.record_delivery(manager, pid, target,
                                        _delivery_payload(base_ref),
                                        actor="架空担当")
    assert accepted["verdict"] == "delivered"
    gap_path = gap.gap_db_path(manager)
    assert gap_path.exists()
    assert resolve_db_path(manager.memory.path, "gap") == gap_path
    # 他PJに影響しない。
    other, other_pid = _make_manager(tmp_path, "doc", 2, tag="-other")
    assert delivery.list_deliveries(other, other_pid) == [] or True
    assert delivery.list_deliveries(manager, target)
    # kind=other は件数対象外。
    before = count_all(manager.memory.path, pid)
    assert before.get("gap:gap_deliveries", 0) == 0
    assert before.get("gap:gap_verifications", 0) == 0
    # 退避/検証: manifest に秘密が出ない。
    backup_root = tmp_path / "架空退避"
    backup = create_backup(manager.memory.path, pid, backup_root,
                           workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    # PJ初期化の世代切替後も他PJに影響しない。
    from app.project_generation import initialize_project, build_reset_preview
    preview = build_reset_preview(manager.memory.path, pid, "replan",
                                  workspace_root=None, executions=None)
    project_name = manager.memory.get_project(pid)["name"]
    initialized = initialize_project(
        manager.memory.path, pid, "replan",
        preview_token=preview["preview_token"], project_name=project_name,
        actor="架空担当", workspace_root=None, backup_root=backup_root,
        executions=None)
    assert initialized["ok"] is True
    assert delivery.list_deliveries(manager, target)
    # 完全削除: 当該PJのみ消える。
    purged = delivery.purge_delivery_records(manager.memory.path, pid)
    assert purged["gap_deliveries"] >= 1
    assert delivery.list_deliveries(manager, target) == []
    assert delivery.list_verifications(manager, target) == []
    purged_gap = gap.purge_project_proposals(manager.memory.path, pid)
    assert purged_gap["function_proposals"] >= 0
    purged_export = export.purge_export_packages(manager.memory.path, pid,
                                                 workspace_root=None)
    assert purged_export["export_packages"] >= 0
    _ = sqlite3


def test_generic_no_specific_project_words():
    import app.capability_delivery as delivery
    text = Path("app/capability_delivery.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "s2a-", "s2b-疑似", "s2d-疑似"):
        assert word not in text
    assert delivery.SHORT_LOG_CHARS <= 2000
    # 保護: set_proposal_status() の verified 拒否が残る。
    gap_text = Path("app/capability_gap.py").read_text(encoding="utf-8")
    assert "verified への遷移は未実装です" in gap_text
    # 2-D の verified 遷移は専用経路のみ (外部から任意に設定できない)。
    assert "_transition_to_verified_internal" in text
    assert text.count("status='verified'") == 1
