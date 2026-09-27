"""Conservative boundary between recoverable inventions and factual/source defects."""
from __future__ import annotations

import json
from typing import Any


_SOURCE = {
    "p0_unreadable_or_zero": (
        "SOURCE_READ_FAILED", "原本の読取失敗または未確認値は原本修正が必要です。",
    ),
    "p0_reconciliation": (
        "SOURCE_RECONCILIATION_FAILED", "原本統制または独立照合の不一致は原本確認が必要です。",
    ),
    "p0_empty_controls": (
        "SOURCE_CONTROLS_MISSING", "独立した原本統制がないため発明で補完できません。",
    ),
    "p0_invoice_incomplete": (
        "SOURCE_INVOICE_INCOMPLETE", "請求書の抽出が未完了のため原本確認が必要です。",
    ),
}


def _data(envelope: dict | None) -> dict:
    if not isinstance(envelope, dict):
        return {}
    value = envelope.get("data") or envelope
    return value if isinstance(value, dict) else {}


def _issues(envelope: dict | None, task: dict | None) -> list[dict]:
    data = _data(envelope)
    values: list[Any] = []
    values.extend((data.get("auto_extraction") or {}).get("issues") or [])
    values.extend(data.get("issues") or [])
    if isinstance(task, dict):
        values.extend(task.get("issues") or [])
    return [item for item in values if isinstance(item, dict)]


def _unresolved(item: dict) -> bool:
    return str(item.get("status") or "").lower() not in {
        "resolved", "completed", "closed", "passed", "verified",
    }


def exclusion_for(error_text: str, envelope: dict | None = None,
                  task: dict | None = None) -> dict | None:
    """Return an existing-rule exclusion; unknown formats retain the old TRIZ path."""
    # Import lazily to avoid an automatic_triz <-> exclusion module import cycle.
    from app.automatic_triz import classify_error_code

    text = str(error_text or "")
    issues = _issues(envelope, task)
    code = classify_error_code(text, envelope)
    if code in _SOURCE:
        exclusion_code, reason = _SOURCE[code]
        return {
            "code": exclusion_code, "family": "SOURCE",
            "reason": reason, "next_owner": "source_fix",
        }
    if any(item.get("kind") == "allocation" and _unresolved(item) for item in issues):
        return {
            "code": "ALLOCATION_UNRESOLVED", "family": "ALLOCATION",
            "reason": "未解決の配賦は業務事実の回答が必要です。",
            "next_owner": "human_fact",
        }
    # Only existing structured dispositions/classifications count as business facts.
    fact = next((item for item in issues if _unresolved(item) and (
        item.get("kind") == "business_fact"
        or item.get("classification") == "business_fact"
        or item.get("disposition") == "business_fact"
    )), None)
    structured = []
    if isinstance(task, dict):
        structured.extend([task.get("classification"), task.get("disposition")])
    structured.extend([_data(envelope).get("classification"), _data(envelope).get("disposition")])
    if fact or "business_fact" in structured:
        return {
            "code": "FACT_MISSING", "family": "FACT",
            "reason": "既存分類で業務事実の不足と判定されています。",
            "next_owner": "human_fact",
        }
    development = "development_required" in structured
    if isinstance(task, dict):
        development = development or task.get("status") == "development_required"
    development = development or _data(envelope).get("status") == "development_required"
    try:
        parsed = json.loads(text)
        development = development or (
            isinstance(parsed, dict) and parsed.get("status") == "development_required"
        )
    except (TypeError, ValueError):
        pass
    if development or text.strip().lower() == "development_required":
        return {
            "code": "DEVELOPMENT_REQUIRED", "family": "DEVELOPMENT",
            "reason": "追加開発が必要と既に判定されています。",
            "next_owner": "development",
        }
    return None
