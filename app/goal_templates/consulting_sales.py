"""Consulting-sales GoalContract definitions (SC01-SC11).

Check *data* only. Evaluation lives in app.generic_goal_checks so other
generic projects can reuse the same machinery.
"""
from __future__ import annotations

import re

TEMPLATE_ID = "consulting_sales/v1"

# Fictional-domain statements. Matching is by topic, not by any real client.
CRITERIA = [
    {
        "criterion_id": "SC01",
        "statement": "対象市場、優先業種、理想顧客像、顧客課題を定義する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["市場", "業種", "顧客像", "課題"],
        "match": r"対象市場|理想顧客像|顧客課題",
        "exec_task_keys": ["SC01"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC02",
        "statement": "コンサルティング商品を複数の契約プランとして設計する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["商品", "契約プラン"],
        "match": r"契約プラン|コンサルティング商品",
        "exec_task_keys": ["SC02"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC03",
        "statement": "各プランの提供範囲、期間、前提条件、価格案、除外事項を整理する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["提供範囲", "価格案", "除外"],
        "match": r"提供範囲|前提条件|価格案|除外事項",
        "exec_task_keys": ["SC03"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC04",
        "statement": "顧客向けの紹介資料、提案書ひな型、ヒアリングシート、PoC計画書、見積ひな型を作成する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["紹介資料", "提案書", "ヒアリング", "PoC", "見積"],
        "match": r"紹介資料|提案書ひな型|ヒアリングシート|PoC計画|見積ひな型",
        "exec_task_keys": ["SC04"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC05",
        "statement": "営業チャネル、案件獲得方法、週次活動量、KPI、収支見込みを含む販売計画を作成する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["販売計画", "KPI", "チャネル"],
        "match": r"販売計画|営業チャネル|週次活動",
        "exec_task_keys": ["SC05"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC06",
        "statement": "見込み客候補を評価し、優先順位と提案仮説を整理する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["見込み客", "優先順位", "提案仮説"],
        "match": r"見込み客候補|提案仮説|優先順位",
        "exec_task_keys": ["SC06"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC07",
        "statement": "承認された見込み客に対する営業文面、商談台本、フォロー文面を作成する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["営業文面", "商談台本", "フォロー"],
        "match": r"営業文面|商談台本|フォロー文面",
        "exec_task_keys": ["SC07"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC08",
        "statement": "商談結果、顧客要件、次回行動、失注理由を記録できる案件管理表を作成する",
        "type": "factual",
        "test_method": "artifact_table",
        "required_evidence": ["result_markdown", "tracker_csv"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["案件管理", "商談結果"],
        "match": r"案件管理表|商談結果",
        "exec_task_keys": ["SC08"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC09",
        "statement": "受注後の要件定義、PoC、受入テスト、教育、運用支援までの標準手順を作成する",
        "type": "factual",
        "test_method": "artifact_content",
        "required_evidence": ["result_markdown"],
        "human_decision_required": False,
        "requires_external_action": False,
        "weight": 1.0,
        "topics": ["標準手順", "PoC", "受入テスト"],
        "match": r"標準手順|受入テスト|運用支援",
        "exec_task_keys": ["SC09"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC10",
        "statement": "実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
        "type": "external_action",
        "test_method": "execution_evidence",
        "required_evidence": ["approved_external_action", "execution_record"],
        "human_decision_required": True,
        "requires_external_action": True,
        "action_kinds": ["approved_external_action"],
        "evidence_fields": ["executed_at", "actor", "target", "result"],
        "weight": 1.4,
        "topics": ["実際の営業活動", "実行済み", "承認待ち"],
        "match": r"実際の営業活動|営業活動について、実行済み",
        "exec_task_keys": ["SC10"],
        "verify_task_keys": ["final_verification"],
    },
    {
        "criterion_id": "SC11",
        "statement": "仮説検証計画に加えて、少なくとも承認済みの試行結果と学習内容の記録がある",
        "type": "external_action",
        "test_method": "trial_learning_evidence",
        "required_evidence": ["hypothesis_plan", "approved_trial_result", "learning_record"],
        "human_decision_required": True,
        "requires_external_action": True,
        "requires_trial_evidence": True,
        "action_kinds": ["approved_external_action"],
        "min_approved_trials": 1,
        "weight": 1.2,
        "topics": ["仮説検証", "試行結果", "学習"],
        "match": r"仮説検証計画|承認済みの試行結果|学習内容",
        "exec_task_keys": ["SC11"],
        "verify_task_keys": ["final_verification"],
    },
]


SALES_MISSION_RE = re.compile(
    r"コンサルティング|営業活動|販売実行|顧客開拓|販売促進|本システムを販売",
    re.I,
)


def is_consulting_sales_mission(mission: dict) -> bool:
    text = f"{mission.get('goal') or ''}\n{mission.get('success_criteria') or ''}"
    return bool(SALES_MISSION_RE.search(text))


def definition_for(criterion: dict) -> dict | None:
    """Return catalog data when the criterion statement matches a sales topic."""
    cid = str(criterion.get("criterion_id") or "")
    statement = str(criterion.get("statement") or "")
    for item in CRITERIA:
        pattern = item.get("match")
        if pattern and re.search(pattern, statement):
            return item
    if cid:
        for item in CRITERIA:
            if item["criterion_id"] == cid and pattern_hits_any(item, statement):
                return item
    return None


def pattern_hits_any(item: dict, statement: str) -> bool:
    topics = item.get("topics") or []
    return any(topic and topic in statement for topic in topics)
