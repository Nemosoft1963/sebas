"""P0-A(4): 外部AI指摘の分類(提案)。疑似PJ・実関数・外部通信なし。

特定の案件の語彙を製品コードに入れない。汎用言い回しの疑似指摘で確認する。
分類は提案であって決定ではない。本文・根拠・元の評価は変えない。
"""
from app.plan_feedback import _p0c_issue_kind
from app.plan_review_loop import (
    ISSUE_CLASSIFICATION_KINDS,
    ISSUE_CLASSIFICATION_VERSION,
    build_issue_bindings,
    classify_external_issue,
)


def _snapshot_and_contract():
    import json

    tasks = []
    criteria = []
    for i in range(6):
        key = "SC00" if i == 0 else f"SC{i:02d}"
        cids = [] if i == 0 else [f"SC{i:02d}"]
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "pseudo step",
            "acceptance_criteria": json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
                "role": "preparation" if key == "SC00" else "execution",
                "internal_preparation_gate_ids": ["PREP00"] if key == "SC00" else [],
            }, ensure_ascii=False),
            "depends_on": [],
        })
        if i >= 1:
            criteria.append({"criterion_id": f"SC{i:02d}",
                             "exec_task_keys": [f"SC{i:02d}"],
                             "verify_task_keys": [f"SC{i:02d}"]})
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-p0a4"}


def test_p0a4_classification_kinds_are_deterministic():
    cases = [
        ("公開用の説明に目的が無く評価できない", "packet_defect"),
        ("日時・連絡先など業務上の事実を確認する必要がある", "missing_business_fact"),
        ("専用の実行器が無く機能追加が必要", "missing_capability"),
        ("確信が持てない・根拠が不十分", "reviewer_uncertain"),
        ("工程の検証方法が欠けている", "plan_defect"),
        ("チェックリストの検証手順が不明瞭です", "plan_defect"),
        ("今日は良い天気ですね", "unclassified"),
    ]
    for text, expected in cases:
        got = classify_external_issue({"text": text, "reason": text})
        assert got["kind"] == expected, (text, got)
        assert got["basis"].startswith("rule:")
        assert got["source"] == "rule"
        assert got["version"] == ISSUE_CLASSIFICATION_VERSION
        assert got["kind"] in ISSUE_CLASSIFICATION_KINDS
        again = classify_external_issue({"text": text, "reason": text})
        assert again == got
    # 本文・根拠・元の評価は変えない(分類だけ別フィールド)。
    issue = {"text": "公開用の説明に目的が無く評価できない", "reason": "参考文"}
    before = dict(issue)
    got = classify_external_issue(issue)
    assert issue == before
    assert got["kind"] == "packet_defect"
    # 分類の失敗は取込全体を止めない(unclassified と理由を記録)。
    broken = classify_external_issue(None)
    assert broken["kind"] == "unclassified"
    assert broken["basis"]


def test_p0a4_binding_keeps_classification_and_p0c_reads_it():
    snap, contract = _snapshot_and_contract()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "2", "unmet_goal": "公開用の説明に目的が無く評価できない",
         "reason": "公開用の説明に目的が無く評価できない",
         "remedy": "目的を追加する", "severity": "major"},
    ]}]
    table = build_issue_bindings(
        parsed, contract, snap, plan_signature="sig-p0a4",
        goal_contract_hash="hash-p0a4", packet_hash="pkt-p0a4")
    assert table["bindings"]
    binding = table["bindings"][0]
    assert isinstance(binding.get("classification"), dict)
    assert binding["classification"]["kind"] == "packet_defect"
    assert binding["classification"]["version"] == ISSUE_CLASSIFICATION_VERSION
    # P0-Cの入口は既存種別が無ければこの分類を読む。
    assert _p0c_issue_kind(binding) == "packet_defect"
    # 既存の種別情報があれば優先し、分類で上書きしない。
    assert _p0c_issue_kind({"kind": "business_fact", "classification": {
        "kind": "packet_defect", "basis": "rule:x", "source": "rule",
        "version": ISSUE_CLASSIFICATION_VERSION}}) == "business_fact"
    # 人の確定や指摘ID・対応先は分類で上書きしない(分類は提案)。
    assert binding["issue_id"]
    assert binding["classification"]["source"] == "rule"


def test_generic_category_does_not_hide_authoritative_classification():
    assert _p0c_issue_kind({"category": "artifact", "text": "公開用の説明に目的が無く評価できない",
                            "binding": {"classification": {"kind": "packet_defect"}}}) == "packet_defect"


def test_classification_routes_are_not_overridden_by_local_action():
    from app.plan_feedback import validate_candidate
    snapshot, _contract = _snapshot_and_contract()
    cases = (
        ("packet_defect", "unresolved", "public_packet"),
        ("missing_business_fact", "business_fact", "fact_check"),
        ("missing_capability", "development", "jenkins_instruction"),
        ("reviewer_uncertain", "unresolved", "reask_evaluator"),
        ("unclassified", "unresolved", "human_review"),
    )
    for kind, disposition, route in cases:
        issue = {"id": "issue-1", "category": "artifact", "text": "疑似の指摘",
                 "binding": {"classification": {"kind": kind, "basis": "rule:test",
                                                "source": "rule", "version": ISSUE_CLASSIFICATION_VERSION}}}
        local_action = {"issue_id": "issue-1", "disposition": "amend", "target": "SC01",
                        "change": "疑似の工程と成果物の照合手順を具体的に追記します。",
                        "reason": "指摘に対応するため具体的な照合手順を追加します。",
                        "classification": {"kind": "plan_defect", "basis": "LLM supplied"}}
        result = validate_candidate({"actions": [local_action]}, [issue], snapshot, None)
        assert result[0]["classification"]["kind"] == kind
        assert result[0]["disposition"] == disposition, kind
        assert result[0]["route"] == route, kind
